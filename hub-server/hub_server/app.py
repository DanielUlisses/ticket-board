"""The hub's FastAPI app: /ws/daemon, /ws/ui, REST under /api, hub-web/dist."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from hub_server import PROTOCOL_VERSION
from hub_server.hub import SEND_ERRORS, UI_SEND_TIMEOUT, DevboxOffline, Hub

REFUSED_CLOSE_CODE = 1008  # policy violation
# hub-server/hub_server/app.py -> <repo>/hub-web/dist
DEFAULT_WEB_DIST = Path(__file__).resolve().parents[2] / "hub-web" / "dist"
# Always accepted as Host besides the configured hub names: local access and
# TestClient.
LOCAL_HOST_NAMES = {"localhost", "127.0.0.1", "::1", "testserver"}
HELLO_TIMEOUT = 10.0
TOKEN_CHECK_INTERVAL = 30.0

log = logging.getLogger("hub_server")


def host_name(host: str) -> str:
    """`Host` header value without its port: tk-hub.x.ts.net, 127.0.0.1, ::1."""
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[1:].partition("]")[0]
    return host.rpartition(":")[0] if host.count(":") == 1 else host


class HostAllowlist:
    """DNS-rebinding guard: refuse any request whose Host isn't one of ours,
    /ws/daemon included (400 on HTTP, close 1008 on websockets)."""

    def __init__(self, app, allowed: set[str]):
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        host = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
        if host_name(host) in self.allowed:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": REFUSED_CLOSE_CODE})
            return
        response = JSONResponse({"detail": "unknown host name"}, 400)
        await response(scope, receive, send)


class TailscaleAllowlist:
    """Admit only the listed `Tailscale-User-Login`s (set by `tailscale serve`)
    to the UI, REST and static files. /ws/daemon is left to its bearer token:
    daemons run on tagged devboxes, which carry no user login."""

    def __init__(self, app, allowed: set[str]):
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or scope["path"] == "/ws/daemon":
            return await self.app(scope, receive, send)
        login = dict(scope["headers"]).get(b"tailscale-user-login", b"").decode()
        if login in self.allowed:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": REFUSED_CLOSE_CODE})
            return
        response = JSONResponse({"detail": "Tailscale login not allowed"}, 403)
        await response(scope, receive, send)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SAME_SITE = {"same-origin", "none"}  # Sec-Fetch-Site values a page of ours sends


def own_origin(scope) -> str:
    """The origin a browser used to reach this request. Behind `tailscale
    serve` the hub sees plain http, so X-Forwarded-Proto and -Host win when
    present. serve (tailscale serve.go v1.84, KB 1312) keeps Host (the ts.net
    name), always sets X-Forwarded-Host, sets X-Forwarded-Proto: https for TLS,
    overwriting whatever the client sent, and passes Origin and the websocket
    upgrade through unchanged. Without serve, a cross-site page still can't set
    them on a WebSocket handshake, or on a POST without a preflight."""
    headers = {k.decode(): v.decode() for k, v in scope["headers"]}
    scheme = {"ws": "http", "wss": "https"}.get(scope["scheme"], scope["scheme"])
    proto = headers.get("x-forwarded-proto", scheme).split(",")[0].strip()
    host = headers.get("x-forwarded-host", headers.get("host", "")).split(",")[0]
    return f"{proto}://{host.strip()}"


class SameOriginGuard:
    """Cross-site request forgery guard for everything a browser reaches.
    /ws/ui: the handshake's Origin must be our own (else close 1008).
    Unsafe HTTP methods: a browser always sends Origin on a cross-site POST,
    so a foreign Origin, or a Sec-Fetch-Site other than same-origin/none, is
    403; neither header means a non-browser client, which is allowed. A
    non-empty body must be JSON (415). New POST routes inherit this."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and scope["path"] == "/ws/ui":
            origin = dict(scope["headers"]).get(b"origin", b"").decode()
            if origin != own_origin(scope):
                await send({"type": "websocket.close", "code": REFUSED_CLOSE_CODE})
                return
        elif scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k.decode(): v.decode() for k, v in scope["headers"]}
            origin = headers.get("origin")
            fetch_site = headers.get("sec-fetch-site")
            if (origin is not None and origin != own_origin(scope)) or (
                fetch_site is not None and fetch_site not in SAME_SITE
            ):
                response = JSONResponse({"detail": "cross-origin request"}, 403)
                return await response(scope, receive, send)
            content_type = headers.get("content-type", "").split(";")[0].strip()
            has_body = "transfer-encoding" in headers or headers.get(
                "content-length", "0"
            ) not in ("", "0")
            if has_body and content_type != "application/json":
                response = JSONResponse({"detail": "send application/json"}, 415)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


class WebApp(StaticFiles):
    """hub-web/dist, with index.html for unknown extension-less paths
    (client-side routes); /api and /ws paths and missing files still 404."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            parts = path.split("/")
            # Only extension-less paths are client-side routes: a missing
            # /assets/x.js must stay a 404.
            if exc.status_code != 404 or parts[0] in ("api", "ws") or "." in parts[-1]:
                raise
            return await super().get_response("index.html", scope)


def bearer(ws: WebSocket) -> str:
    scheme, _, token = ws.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class ArchiveRequest(BaseModel):
    archived: bool


class ScanRequest(BaseModel):
    devbox: str | None = None


class PolicyValues(BaseModel):
    """The board-override keys the UI may write; box-level keys (max_parallel,
    usage_gate_pct, synthesis) are not among them. An explicit null removes the board
    override of that key (it inherits again)."""

    model_config = ConfigDict(extra="forbid")
    forge: Literal["github", "azure", "none"] | None = None
    require_human_before_pr: bool | None = None
    auto_merge: bool | None = None
    night_shift: bool | None = None
    autonomy: Literal["assist", "auto"] | None = None


class PolicyRequest(BaseModel):
    values: PolicyValues


class MemoryProposal(BaseModel):
    text: str


def create_app(
    db_path: Path | str,
    *,
    allowed_logins: Iterable[str] | None = None,
    web_dist: Path | str | None = DEFAULT_WEB_DIST,
    rpc_timeouts: dict[str, float] | None = None,
    host_names: Iterable[str] = (),
    hello_timeout: float = HELLO_TIMEOUT,
    token_check_interval: float = TOKEN_CHECK_INTERVAL,
    ui_send_timeout: float = UI_SEND_TIMEOUT,
) -> FastAPI:
    """allowed_logins: Tailscale-User-Login allowlist; empty or None admits the
    whole tailnet. web_dist: the built hub-web, served at / when it exists.
    rpc_timeouts: per-method rpc timeout overrides, in seconds. host_names:
    the hub's own names (and IPs) accepted as Host, besides local ones.
    Timeouts and the token re-check interval are in seconds."""
    hub = Hub(db_path, rpc_timeouts=rpc_timeouts, ui_send_timeout=ui_send_timeout)
    app = FastAPI(title="tk-hub")
    app.add_middleware(SameOriginGuard)
    if allowed_logins:
        app.add_middleware(TailscaleAllowlist, allowed=set(allowed_logins))
    # Added last, so it runs first.
    configured = {host_name(h) for h in host_names}
    allowed_hosts = LOCAL_HOST_NAMES | configured
    if configured:
        log.info("accepting Host: %s", ", ".join(sorted(allowed_hosts)))
    else:
        log.warning(
            "no --host-name / TK_HUB_HOSTNAMES set: only local names are accepted "
            "as Host (%s); set the hub's ts.net name to reach it over the tailnet",
            ", ".join(sorted(LOCAL_HOST_NAMES)),
        )
    app.add_middleware(HostAllowlist, allowed=allowed_hosts)
    app.state.hub = hub

    async def refuse(ws: WebSocket, reason: str):
        with contextlib.suppress(*SEND_ERRORS):
            await ws.send_json(
                {
                    "type": "refused",
                    "protocol_version": PROTOCOL_VERSION,
                    "reason": reason,
                }
            )
            await ws.close(REFUSED_CLOSE_CODE)

    async def receive_message(ws: WebSocket):
        """The next frame parsed as JSON; WebSocketDisconnect when the socket
        closes; ValueError when the frame isn't JSON (or nests too deep)."""
        frame = await ws.receive()
        if frame["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(frame.get("code", 1000))
        text = frame.get("text")
        if text is None:
            text = (frame.get("bytes") or b"").decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except RecursionError as exc:
            raise ValueError("JSON nested too deeply") from exc

    @app.websocket("/ws/daemon")
    async def daemon_socket(ws: WebSocket):
        await ws.accept()
        token = bearer(ws)
        try:
            hello = await asyncio.wait_for(receive_message(ws), hello_timeout)
        except WebSocketDisconnect:
            return
        except TimeoutError:
            await refuse(ws, f"no hello within {hello_timeout:g}s")
            return
        except ValueError:
            hello = None
        reason = await hub.admit(hello, token)
        if reason:
            await refuse(ws, reason)
            return
        await ws.send_json({"type": "welcome", "protocol_version": PROTOCOL_VERSION})
        devbox = hello["devbox"]
        await hub.connected(devbox, ws, hello)

        # `hub-server token revoke` runs in another process: the store is
        # re-checked on every message and every token_check_interval. The
        # watchdog only closes the socket; the loop below sees the close and
        # its `finally` is the one caller of hub.disconnected (a watchdog
        # cancelled mid-broadcast would leave the UI showing the box online).
        async def watch_token():
            while True:
                await asyncio.sleep(token_check_interval)
                if not hub.store.check_token(devbox, token):
                    await refuse(ws, "token revoked")
                    return

        watchdog = asyncio.create_task(watch_token())
        try:
            while True:
                try:
                    message, broken = await receive_message(ws), None
                except ValueError as exc:
                    message, broken = None, exc
                if not hub.store.check_token(devbox, token):
                    await refuse(ws, "token revoked")
                    break
                if broken:
                    log.warning("ignoring a non-JSON frame from %s: %s", devbox, broken)
                    continue
                await hub.received(devbox, ws, message)
        except WebSocketDisconnect:
            pass
        finally:
            watchdog.cancel()
            await hub.disconnected(devbox, ws)

    @app.websocket("/ws/ui")
    async def ui_socket(ws: WebSocket):
        await ws.accept()
        hub.ui.add(ws)
        try:
            while True:
                await ws.receive_text()  # the UI only listens; drain pings
        except WebSocketDisconnect:
            pass
        finally:
            hub.ui.discard(ws)

    @app.get("/api/boards")
    async def list_boards():
        return {
            "boards": hub.store.boards.all(),
            "devboxes": hub.store.devbox_statuses(),
        }

    def find_ticket(board_key: str, nn: str) -> dict:
        board = hub.store.boards.get(board_key)
        found = board and next((t for t in board["tickets"] if t["nn"] == nn), None)
        if not found:
            raise HTTPException(404, f"no ticket {nn} on board {board_key}")
        return found

    # board_key holds slashes (`<devbox>/<repo>/<board>`), hence `:path`.
    @app.get("/api/boards/{board_key:path}/tickets/{nn}/activity")
    async def ticket_activity(board_key: str, nn: str):
        find_ticket(board_key, nn)
        return {
            "board_key": board_key,
            "nn": nn,
            "events": hub.store.boards.activity(board_key, nn),
        }

    @app.get("/api/boards/{board_key:path}/tickets/{nn}/report")
    async def ticket_report(board_key: str, nn: str):
        found = find_ticket(board_key, nn)
        return {
            "board_key": board_key,
            "nn": nn,
            "title": found["title"],
            "full_report_md": found.get("full_report_md"),
            # The ticket file's raw text, when the daemon sends it as `body`
            # (an extra ticket property; the schema allows them).
            "body": found.get("body"),
        }

    @app.post("/api/boards/{board_key:path}/policy", status_code=202)
    async def set_policy(board_key: str, request: PolicyRequest):
        """set_policy on the board's devbox: tk.sh policy-set writes the board
        override, the board's next snapshot carries the result."""
        board = hub.store.boards.get(board_key)
        if not board:
            raise HTTPException(404, f"no board {board_key}")
        values = request.values.model_dump(exclude_unset=True)
        if not values:
            raise HTTPException(422, "no policy values to set")
        try:
            rpc_id = await hub.send_rpc(
                board["devbox"],
                "set_policy",
                {"board_key": board_key, "values": values},
                board_key=board_key,
            )
        except DevboxOffline:
            raise HTTPException(
                404, f"devbox {board['devbox']} is not connected"
            ) from None
        return {"rpc": {"id": rpc_id, "devbox": board["devbox"]}}

    @app.post("/api/boards/{board_key:path}/memory", status_code=202)
    async def propose_memory(board_key: str, request: MemoryProposal):
        """propose_memory on the board's devbox: the daemon appends a project-memory
        ticket to the board. The hub never writes project-memory.md itself."""
        board = hub.store.boards.get(board_key)
        if not board:
            raise HTTPException(404, f"no board {board_key}")
        text = request.text.strip()
        if not text:
            raise HTTPException(422, "empty memory rule")
        try:
            rpc_id = await hub.send_rpc(
                board["devbox"],
                "propose_memory",
                {"board_key": board_key, "text": text},
                board_key=board_key,
            )
        except DevboxOffline:
            raise HTTPException(
                404, f"devbox {board['devbox']} is not connected"
            ) from None
        return {"rpc": {"id": rpc_id, "devbox": board["devbox"]}}

    @app.post("/api/scan", status_code=202)
    async def scan(request: ScanRequest | None = None):
        """rescan_boards on one devbox, or on every connected one; results
        arrive on /ws/ui as rpc_result."""
        devbox = request.devbox if request else None
        try:
            rpcs = await hub.scan(devbox)
        except DevboxOffline:
            raise HTTPException(404, f"devbox {devbox} is not connected") from None
        return {"rpcs": rpcs}

    @app.post("/api/boards/{board_key:path}/archive", status_code=202)
    async def archive(board_key: str, request: ArchiveRequest):
        """set_archived on the board's devbox (box online only); the daemon
        re-exports, so the flag reaches the hub in the next snapshot."""
        board = hub.store.boards.get(board_key)
        if board is None:
            raise HTTPException(404, f"no board {board_key}")
        devbox = board["devbox"]
        try:
            rpc_id = await hub.send_rpc(
                devbox,
                "set_archived",
                {"board_key": board_key, "archived": request.archived},
                board_key=board_key,
            )
        except DevboxOffline:
            raise HTTPException(409, f"devbox {devbox} is offline") from None
        return {"rpcs": [{"id": rpc_id, "devbox": devbox}]}

    # Last, so /api and /ws routes win over the catch-all mount.
    if web_dist is not None and Path(web_dist).is_dir():
        app.mount("/", WebApp(directory=web_dist, html=True), name="web")

    return app
