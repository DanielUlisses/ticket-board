"""tk-daemon.py as a process against an in-test fake hub on /ws/daemon (TLS,
bearer token), with the real tk-export.sh over fake tk.sh / herdr / op."""

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys

import pytest
from conftest import DAEMON_DIR, row, schema_errors
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

DAEMON = DAEMON_DIR / "tk-daemon.py"
TOKEN = "tok-3f9c1d2e-not-a-real-secret"
TOKEN_REF = "op://Devboxes/tk-hub/dev-test"
NAME = "hub.test"  # the hub's MagicDNS name, as in the certificate


@pytest.fixture(scope="session")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-keyout", d / "key.pem", "-out", d / "cert.pem", "-days", "2",
         "-subj", f"/CN={NAME}", "-addext", f"subjectAltName=DNS:{NAME}",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,digitalSignature,keyCertSign"],
        check=True, capture_output=True,
    )  # fmt: skip
    return d / "cert.pem", d / "key.pem"


class FakeHub:
    """Accepts a daemon with the right bearer token, answers hello with welcome,
    and queues every message it receives."""

    def __init__(self, cert, reject_first: int = 0):
        import ssl

        self.reject_first = reject_first  # answer this many handshakes with HTTP 401

        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(*cert)
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.connections = 0
        self.ws = None

    def _auth(self, connection, request):
        if self.reject_first > 0:
            self.reject_first -= 1
            return connection.respond(401, "not yet\n")
        if (
            request.path != "/ws/daemon"
            or request.headers.get("Authorization") != f"Bearer {TOKEN}"
        ):
            return connection.respond(401, "bad token\n")

    async def _handler(self, ws):
        self.ws = ws
        self.connections += 1
        with contextlib.suppress(ConnectionClosed):  # the daemon is killed at the end
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "hello":
                    await ws.send(
                        json.dumps({"type": "welcome", "protocol_version": "1.0"})
                    )
                await self.inbox.put(msg)

    async def __aenter__(self):
        self.server = await serve(
            self._handler, "127.0.0.1", 0, ssl=self.ctx, process_request=self._auth
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def next(self, type: str | None = None, timeout: float = 15) -> dict:
        """The next message (of `type`, skipping others)."""
        while True:
            msg = await asyncio.wait_for(self.inbox.get(), timeout)
            assert schema_errors(msg) == [], msg
            if type is None or msg["type"] == type:
                return msg

    async def send(self, msg: dict):
        await self.ws.send(json.dumps(msg))


POLICY = {"forge": "none", "require_human_before_pr": True, "auto_merge": False,
          "night_shift": False, "autonomy": "auto", "max_parallel": 5,
          "usage_gate_pct": 90, "synthesis": {"backends": ["agy"], "timeout": 300}}  # fmt: skip


@contextlib.asynccontextmanager
async def daemon(box, hub, cert, name=NAME, **config):
    box.write(".config/tk-daemon/config.json", {
        "url": f"wss://{name}:{hub.port}/ws/daemon", "ip": "127.0.0.1",
        "token_ref": TOKEN_REF, "devbox": "dev-test",
        "export_interval_s": 0.3, "backoff_initial_s": 0.1, "backoff_max_s": 0.5,
        **config})  # fmt: skip
    env = {**box.env, "SSL_CERT_FILE": str(cert[0]), "FAKE_OP_TOKEN": TOKEN,
           "FAKE_OP_LOG": str(box.tmp / "op.log")}  # fmt: skip
    box.daemon_log = box.tmp / "daemon.log"
    with open(box.daemon_log, "wb") as log:  # noqa: ASYNC230 — a local file, once
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(DAEMON), env=env, stdout=log, stderr=log
        )
        try:
            yield proc
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def test_connects_to_the_ip_with_tls_for_the_name_and_the_op_token_sends_hello_then_full_snapshot(
    box, cert
):
    box.board("alpha", "tk-hub", [row("01", "working")])
    box.board("beta", "zig", [row("02")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert):
            hello = await hub.next()
            snap = await hub.next()
        return hello, snap

    hello, snap = run(scenario())

    assert hello["type"] == "hello" and hello["devbox"] == "dev-test"
    assert hello["health"]["daemon_version"] == "0.1.0"
    assert "status: running" in hello["health"]["herdr"]["status"]
    assert (snap["type"], snap["full"]) == ("snapshot", True)
    assert [b["key"] for b in snap["boards"]] == [
        "dev-test/alpha/tk-hub",
        "dev-test/beta/zig",
    ]
    assert (box.tmp / "op.log").read_text().splitlines()[0] == TOKEN_REF
    # The token lives in memory only: not in the daemon's output, not on disk under HOME.
    assert TOKEN not in box.daemon_log.read_text()
    for f in box.home.rglob("*"):
        assert not f.is_file() or TOKEN.encode() not in f.read_bytes(), f


async def quiet(hub, seconds: float) -> bool:
    """True when nothing arrives for `seconds`."""
    try:
        msg = await asyncio.wait_for(hub.inbox.get(), seconds)
    except TimeoutError:
        return True
    pytest.fail(f"unexpected message: {msg}")


def test_refuses_a_hub_whose_certificate_is_not_for_the_configured_name(box, cert):
    box.board("alpha", "b", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, name="other.test"):
            await asyncio.sleep(1.5)
            return hub.connections

    assert run(scenario()) == 0
    assert "certificate verify failed" in box.daemon_log.read_text()


def test_after_the_full_snapshot_only_changed_boards_are_sent(box, cert):
    box.board("alpha", "same", [row("01")])
    changing = box.board("alpha", "changing", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.2):
            await hub.next("snapshot")
            assert await quiet(hub, 1.0)  # several ticks, nothing changed, nothing sent
            (changing / "hub-meta.json").write_text(json.dumps({"archived": True}))
            return await hub.next()

    delta = run(scenario())

    assert (delta["type"], delta["full"], delta["removed"]) == ("snapshot", False, [])
    assert [b["key"] for b in delta["boards"]] == ["dev-test/alpha/changing"]
    assert delta["boards"][0]["hub_meta"]["archived"] is True


def test_rescan_boards_sends_a_full_snapshot_with_new_boards_and_removed_ones(
    box, cert
):
    import shutil

    box.board("alpha", "stays", [row("01")])
    gone = box.board("alpha", "vanishes", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            shutil.rmtree(gone)
            box.board("beta", "new", [row("02")])
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return await hub.next(), await hub.next()

    snap, result = run(scenario())

    assert (snap["type"], snap["full"]) == ("snapshot", True)
    assert [b["key"] for b in snap["boards"]] == [
        "dev-test/alpha/stays",
        "dev-test/beta/new",
    ]
    assert snap["removed"] == ["dev-test/alpha/vanishes"]
    assert result["type"] == "rpc_result"
    assert (result["id"], result["ok"], result["exit_code"]) == ("r-1", True, 0)
    assert "dev-test/alpha/vanishes" in result["output_tail"]


def test_set_policy_writes_the_board_override_through_tk_sh_and_the_change_round_trips(
    box, cert
):
    box.env["FAKE_TK_POLICY"] = "1"
    b = box.board("alpha", "b", [row("01")], {"resolved-policy.json": POLICY})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(
                {
                    "type": "rpc",
                    "id": "r-p",
                    "method": "set_policy",
                    "params": {
                        "board_key": "dev-test/alpha/b",
                        "values": {"autonomy": "assist", "auto_merge": True},
                    },
                }
            )
            return await hub.next("rpc_result"), await hub.next("snapshot")

    result, snap = run(scenario())

    assert (result["id"], result["ok"], result["exit_code"]) == ("r-p", True, 0)
    call = [c for c in box.tk_calls() if c["argv"][0] == "policy-set"]
    assert [c["argv"] for c in call] == [
        ["policy-set", str(b), 'autonomy="assist"', "auto_merge=true"]
    ]
    assert call[0]["cwd"] == str(b.parent.parent)  # the repo's main checkout
    assert json.loads((b / "policy.json").read_text()) == {
        "autonomy": "assist", "auto_merge": True}  # fmt: skip
    policy = snap["boards"][0]["policy"]
    assert (policy["autonomy"], policy["auto_merge"]) == ("assist", True)


def test_set_policy_failures_are_reported_with_tk_shs_message(box, cert):
    box.env["FAKE_TK_POLICY"] = "1"
    box.board("alpha", "b", [row("01")], {"resolved-policy.json": POLICY,
                                          "policy-set-fails": ""})  # fmt: skip

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send({"type": "rpc", "id": "r-p", "method": "set_policy",
                            "params": {"board_key": "dev-test/alpha/b", "values": {"autonomy": "auto"}}})  # fmt: skip
            await hub.send({"type": "rpc", "id": "r-q", "method": "set_policy",
                            "params": {"board_key": "dev-test/alpha/nope", "values": {"autonomy": "auto"}}})  # fmt: skip
            return await hub.next("rpc_result"), await hub.next("rpc_result")

    results = {r["id"]: r for r in run(scenario())}
    failed, unknown = results["r-p"], results["r-q"]

    assert (failed["ok"], failed["exit_code"]) == (False, 1)
    assert "not written" in failed["output_tail"]
    assert unknown["ok"] is False and "no board" in unknown["reason"]


def test_set_policy_null_sends_an_empty_value_that_removes_the_override(box, cert):
    box.env["FAKE_TK_POLICY"] = "1"
    b = box.board(
        "alpha",
        "b",
        [row("01")],
        {"resolved-policy.json": POLICY, "policy.json": {"autonomy": "assist", "auto_merge": True}},
    )  # fmt: skip

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send({"type": "rpc", "id": "r-n", "method": "set_policy",
                            "params": {"board_key": "dev-test/alpha/b", "values": {"autonomy": None}}})  # fmt: skip
            return await hub.next("rpc_result")

    result = run(scenario())

    assert result["ok"] is True
    call = [c["argv"] for c in box.tk_calls() if c["argv"][0] == "policy-set"]
    assert call == [["policy-set", str(b), "autonomy="]]
    assert json.loads((b / "policy.json").read_text()) == {"auto_merge": True}


@pytest.mark.parametrize(
    "values",
    [
        {"max_parallel": 3},
        {"synthesis": {"backends": []}},
        {"autonomy": "turbo"},
        {"forge": "gitlab"},
        {"auto_merge": "true"},
        {"night_shift": 1},
        {"autonomy": "auto", "usage_gate_pct": 50},
    ],
)
def test_set_policy_refuses_unknown_keys_and_bad_values_without_calling_tk_sh(
    box, cert, values
):
    box.env["FAKE_TK_POLICY"] = "1"
    box.board("alpha", "b", [row("01")], {"resolved-policy.json": POLICY})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send({"type": "rpc", "id": "r-bad", "method": "set_policy",
                            "params": {"board_key": "dev-test/alpha/b", "values": values}})  # fmt: skip
            return await hub.next("rpc_result")

    result = run(scenario())

    assert result["ok"] is False and result["reason"]
    assert [c for c in box.tk_calls() if c["argv"][0] == "policy-set"] == []


def test_an_rpc_past_its_timeout_is_killed_and_reported_and_the_daemon_carries_on(
    box, cert
):
    slow = box.board("alpha", "b", [row("01")]) / "export-slow"

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=600, rpc_timeout_s=0.5),
        ):
            await hub.next("snapshot")
            slow.write_text("5")
            await hub.send(
                {"type": "rpc", "id": "r-slow", "method": "rescan_boards", "params": {}}
            )
            timed_out = await hub.next("rpc_result", timeout=3)
            slow.unlink()
            await hub.send(
                {
                    "type": "rpc",
                    "id": "r-again",
                    "method": "rescan_boards",
                    "params": {},
                }
            )
            return timed_out, await hub.next("rpc_result")

    timed_out, again = run(scenario())

    assert timed_out == {"type": "rpc_result", "id": "r-slow", "ok": False, "exit_code": None,
                         "output_tail": "", "reason": "timed out after 0.5s"}  # fmt: skip
    assert (again["id"], again["ok"]) == ("r-again", True)


def test_a_board_whose_export_fails_keeps_its_last_copy_and_is_not_removed(box, cert):
    b = box.board("alpha", "b", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            first = await hub.next("snapshot")
            (b / "hub-meta.json").write_text(json.dumps({"tickets": []}))
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return first, await hub.next("snapshot")

    first, snap = run(scenario())

    assert snap["removed"] == []
    assert snap["boards"] == first["boards"]


def test_a_hung_periodic_export_is_killed_and_the_next_tick_carries_on(box, cert):
    b = box.board("alpha", "b", [row("01")])

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=0.2, export_timeout_s=0.5),
        ):
            await hub.next("snapshot")
            (b / "export-stubborn").write_text("30")  # outlives the deadline
            await asyncio.sleep(1.5)  # at least one tick times out
            (b / "export-stubborn").unlink()
            (b / "hub-meta.json").write_text(json.dumps({"archived": True}))
            return await hub.next("snapshot", timeout=5)

    delta = run(scenario())

    assert [x["key"] for x in delta["boards"]] == ["dev-test/alpha/b"]
    assert delta["boards"][0]["hub_meta"]["archived"] is True
    assert "timed out after 0.5s" in box.daemon_log.read_text()


def test_a_failed_export_before_hello_still_connects_then_sends_the_full_snapshot(
    box, cert
):
    stubborn = box.board("alpha", "b", [row("01")]) / "export-stubborn"
    stubborn.write_text("30")

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_timeout_s=0.5, export_interval_s=0.3),
        ):
            hello = await hub.next(timeout=5)
            stubborn.unlink()
            return hello, await hub.next("snapshot", timeout=5)

    hello, snap = run(scenario())

    assert hello["type"] == "hello"
    assert hello["health"]["herdr"]["ok"] is False
    assert hello["health"]["usage"]["state"] == "unknown"
    assert (snap["full"], [b["key"] for b in snap["boards"]]) == (
        True,
        ["dev-test/alpha/b"],
    )
    assert "timed out after 0.5s" in box.daemon_log.read_text()


def test_slow_boards_past_the_export_budget_are_failed_not_the_whole_export(box, cert):
    slugs = ["a", "b", "c", "d", "e", "f"]
    dirs = [box.board("alpha", s, [row("01")]) for s in slugs]

    async def scenario():
        async with FakeHub(cert) as hub, daemon(
            box, hub, cert, export_interval_s=600, export_timeout_s=1.4, rpc_timeout_s=10,
        ):  # fmt: skip
            await hub.next("snapshot")
            for d in dirs:
                (d / "export-slow").write_text("0.35")
                (d / "hub-meta.json").write_text(json.dumps({"archived": True}))
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return (
                await hub.next("snapshot", timeout=6),
                await hub.next("rpc_result"),
            )

    snap, result = run(scenario())

    assert result["ok"] is True
    assert snap["removed"] == []
    archived = [b["hub_meta"]["archived"] for b in snap["boards"]]
    assert len(archived) == len(slugs)
    assert True in archived and False in archived  # some exported, the rest kept
    assert "tk-export.sh timed out" not in box.daemon_log.read_text()


def test_rpcs_outside_the_daemons_list_are_rejected_without_running_anything(box, cert):
    box.board("alpha", "b", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            calls = len(box.tk_calls())
            await hub.send(
                {"type": "rpc", "id": "r-2", "method": "launch_ticket", "params": {}}
            )
            await hub.ws.send(
                json.dumps(
                    {"type": "rpc", "id": "r-3", "method": "rm -rf", "params": {}}
                )
            )
            results = [await hub.next("rpc_result"), await hub.next("rpc_result")]
            assert await quiet(hub, 0.5)
            return results, len(box.tk_calls()) - calls

    results, new_calls = run(scenario())

    assert sorted(r["id"] for r in results) == ["r-2", "r-3"]
    for r in results:
        assert (r["ok"], r["exit_code"], r["output_tail"]) == (False, None, "")
        assert "unknown rpc" in r["reason"]
    assert new_calls == 0


def test_reconnects_after_the_hub_drops_and_starts_again_with_hello_and_full_snapshot(
    box, cert
):
    box.board("alpha", "b", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.ws.close()
            hello = await hub.next()
            snap = await hub.next()
            return hello, snap, hub.connections

    hello, snap, connections = run(scenario())

    assert (hello["type"], snap["type"], snap["full"], connections) == (
        "hello",
        "snapshot",
        True,
        2,
    )
    assert [b["key"] for b in snap["boards"]] == ["dev-test/alpha/b"]


def test_a_rejected_handshake_rereads_the_token_and_retries_with_backoff(box, cert):
    box.board("alpha", "b", [row("01")])

    async def scenario():
        async with FakeHub(cert, reject_first=2) as hub, daemon(box, hub, cert):
            return await hub.next()

    assert run(scenario())["type"] == "hello"
    assert (box.tmp / "op.log").read_text().splitlines() == [TOKEN_REF] * 3
    log = box.daemon_log.read_text()
    assert log.count("HTTP 401") == 2 and log.count("reconnecting in") >= 2
    assert TOKEN not in log


def test_a_repo_whose_board_list_is_malformed_keeps_its_boards_last_copies(box, cert):
    box.board("alpha", "one", [row("01")])
    box.board("beta", "two", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            first = await hub.next("snapshot")
            (box.work / "alpha" / ".scratch" / "boards-out").write_text("not json")
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return first, await hub.next("snapshot")

    first, snap = run(scenario())

    assert snap["removed"] == []
    assert snap["boards"][0] == first["boards"][0]  # alpha/one: its last copy
    assert [b["key"] for b in snap["boards"]] == [
        "dev-test/alpha/one",
        "dev-test/beta/two",
    ]


def test_a_hung_board_times_out_alone_and_the_others_still_sync(box, cert):
    hung = box.board("alpha", "hung", [row("01")])
    moving = box.board("alpha", "moving", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(
            box, hub, cert, export_interval_s=600, export_call_timeout_s=1,
            export_timeout_s=20, rpc_timeout_s=40,
        ):  # fmt: skip
            first = await hub.next("snapshot")
            (hung / "export-slow").write_text("30")
            (moving / "hub-meta.json").write_text(json.dumps({"archived": True}))
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return first, await hub.next("snapshot", timeout=10)

    first, snap = run(scenario())

    boards = {b["key"]: b for b in snap["boards"]}
    assert snap["removed"] == []
    assert boards["dev-test/alpha/hung"] == first["boards"][0]
    assert boards["dev-test/alpha/moving"]["hub_meta"]["archived"] is True
    assert "tk-export: failed dev-test/alpha/hung" in box.daemon_log.read_text()


def test_rescan_waits_for_a_running_export_no_longer_than_its_budget(box, cert):
    slow = box.board("alpha", "b", [row("01")]) / "export-slow"

    async def scenario():
        async with FakeHub(cert) as hub, daemon(
            box, hub, cert, export_interval_s=0.2, export_timeout_s=5, rpc_timeout_s=6,
        ):  # fmt: skip
            await hub.next("snapshot")
            slow.write_text("4")
            await asyncio.sleep(0.8)  # a tick is now in its slow export
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return await hub.next("rpc_result", timeout=3)

    result = run(scenario())

    assert (result["id"], result["ok"]) == ("r-1", False)
    assert "export already running" in result["reason"]


def alive(pid: int) -> bool:
    try:
        stat = open(f"/proc/{pid}/stat").read()  # noqa: SIM115
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_sigterm_stops_the_daemon_and_kills_a_running_export(box, cert):
    slow = box.board("alpha", "b", [row("01")]) / "export-slow"

    async def scenario():
        async with (
            FakeHub(cert) as hub,
            daemon(box, hub, cert, export_interval_s=0.2, export_timeout_s=60) as proc,
        ):
            await hub.next("snapshot")
            slow.write_text("60")
            await asyncio.sleep(1)  # a tick is now in its slow export
            pids = [c["pid"] for c in box.tk_calls() if c["argv"][0] == "export"]
            assert alive(pids[-1])
            proc.terminate()
            rc = await asyncio.wait_for(proc.wait(), 5)
            for _ in range(30):
                if not alive(pids[-1]):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.2)
            return rc, alive(pids[-1]), hub.ws.close_code

    rc, still_alive, close_code = run(scenario())

    assert rc == 0
    assert not still_alive
    assert close_code == 1001


def test_a_fresh_daemon_past_its_deadline_sends_every_board_over_a_few_exports(
    box, cert
):
    slugs = ["a", "b", "c", "d", "e", "f"]
    for s in slugs:
        box.board("alpha", s, [row("01")], {"export-slow": "0.35"})

    async def scenario():
        seen, removed = set(), []
        async with FakeHub(cert) as hub, daemon(
            box, hub, cert, export_interval_s=0.2, export_timeout_s=1.4, rpc_timeout_s=10,
        ):  # fmt: skip
            while len(seen) < len(slugs):
                snap = await hub.next("snapshot", timeout=10)
                if snap["full"]:  # never one that would make the hub drop a board
                    assert seen <= {b["key"] for b in snap["boards"]}
                seen.update(b["key"] for b in snap["boards"])
                removed += snap["removed"]
        return seen, removed

    seen, removed = run(scenario())

    assert seen == {f"dev-test/alpha/{s}" for s in slugs}
    assert removed == []


def test_a_schema_invalid_board_is_failed_keeps_its_last_copy_and_the_rest_are_sent(
    box, cert
):
    good = box.board("alpha", "good", [row("01")])
    bad = box.board("alpha", "bad", [row("01")])
    box.board("alpha", "never", [row("01")], {"policy.json": {"max_parallel": "5"}})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            first = await hub.next("snapshot")  # FakeHub checks every message
            (bad / "policy.json").write_text(json.dumps({"max_parallel": "5"}))
            (good / "hub-meta.json").write_text(json.dumps({"archived": True}))
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return first, await hub.next("snapshot"), await hub.next("rpc_result")

    first, snap, result = run(scenario())

    # `never` has failed since the start, with no copy to send: no snapshot may be
    # full (the hub would remove it), so both go as deltas.
    assert (first["full"], snap["full"]) == (False, False)
    assert [b["key"] for b in first["boards"]] == [
        "dev-test/alpha/bad",
        "dev-test/alpha/good",
    ]
    assert [b["key"] for b in snap["boards"]] == ["dev-test/alpha/good"]
    assert snap["boards"][0]["hub_meta"]["archived"] is True
    assert snap["removed"] == []
    assert (
        "delta" in result["output_tail"]
        and "dev-test/alpha/never" in result["output_tail"]
    )
    log = box.daemon_log.read_text()
    assert "dev-test/alpha/never" in log and "schema" in log


def test_after_a_restart_a_failing_board_holds_back_full_snapshots_until_it_exports(
    box, cert
):
    box.board("alpha", "good", [row("01")])
    broken = box.board("alpha", "broken", [row("01")], {"hub-meta.json": "{oops"})

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=0.3):
            first = await hub.next("snapshot")
            (broken / "hub-meta.json").write_text(json.dumps({"archived": False}))
            while True:
                snap = await hub.next("snapshot", timeout=5)
                if snap["full"]:
                    return first, snap

    first, full = run(scenario())

    assert (first["full"], [b["key"] for b in first["boards"]]) == (
        False,
        ["dev-test/alpha/good"],
    )
    assert [b["key"] for b in full["boards"]] == [
        "dev-test/alpha/broken",
        "dev-test/alpha/good",
    ]


def test_a_failed_board_whose_key_differs_from_its_dir_name_keeps_its_last_copy(
    box, cert
):
    d = box.board("alpha", "--dashed", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            first = await hub.next("snapshot")
            (d / "export-fails").write_text("")
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "rescan_boards", "params": {}}
            )
            return first, await hub.next("snapshot")

    first, snap = run(scenario())

    assert [b["key"] for b in first["boards"]] == ["dev-test/alpha/dashed"]
    assert snap["removed"] == []
    assert snap["boards"] == first["boards"]


def test_the_daemons_schema_check_agrees_with_jsonschema_on_every_fixture():
    import importlib.util

    from conftest import PROTOCOL, VALIDATOR

    spec = importlib.util.spec_from_file_location("tk_daemon", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check = mod.Schema(json.loads((PROTOCOL / "schema.json").read_text()))
    files = sorted(PROTOCOL.glob("fixtures*/*.json"))
    assert len(files) > 40
    for f in files:
        msg = json.loads(f.read_text())
        assert (check.errors(msg) == []) == VALIDATOR.is_valid(msg), f.name


def test_set_archived_writes_hub_meta_keeping_other_keys_and_re_exports(box, cert):
    d = box.board("alpha", "b", [row("01")])
    (d / "hub-meta.json").write_text(json.dumps({"tickets": {"01": {"x": 1}}}))

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            out = []
            for i, flag in enumerate([True, False]):
                params = {"board_key": "dev-test/alpha/b", "archived": flag}
                await hub.send(
                    {"type": "rpc", "id": f"r-{i}", "method": "set_archived",
                     "params": params}
                )  # fmt: skip
                result = await hub.next("rpc_result")
                snap = await hub.next("snapshot")
                out.append(
                    (result, snap, json.loads((d / "hub-meta.json").read_text()))
                )
            return out

    (r1, s1, m1), (_r2, s2, m2) = run(scenario())

    assert r1["ok"] is True
    assert m1 == {"archived": True, "tickets": {"01": {"x": 1}}}
    assert s1["boards"][0]["hub_meta"]["archived"] is True
    assert m2["archived"] is False and m2["tickets"] == {"01": {"x": 1}}
    assert s2["boards"][0]["hub_meta"]["archived"] is False


def test_set_archived_refuses_an_unknown_board(box, cert):
    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "set_archived",
                 "params": {"board_key": "dev-test/alpha/nope", "archived": True}}
            )  # fmt: skip
            return await hub.next("rpc_result")

    result = run(scenario())

    assert result["ok"] is False
    assert "nope" in result["reason"]


def _set_archived(box, cert, params, before_rpc=None):
    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            if before_rpc:
                before_rpc()
            await hub.send(
                {"type": "rpc", "id": "r-1", "method": "set_archived", "params": params}
            )  # fmt: skip
            return await hub.next("rpc_result")

    return run(scenario())


@pytest.mark.parametrize(
    "params",
    [
        {"board_key": "dev-test/alpha/b", "archived": "yes"},
        {"board_key": "dev-test/alpha/b", "archived": 1},
        {"board_key": "dev-test/alpha/b"},
        {"board_key": "dev-test/alpha/../b", "archived": True},
        {"board_key": "dev-test/alpha/", "archived": True},
        {"board_key": "dev-test/other/b", "archived": True},
        {"board_key": 7, "archived": True},
    ],
)
def test_set_archived_refuses_bad_params_or_unknown_key(box, cert, params):
    d = box.board("alpha", "b", [row("01")])

    result = _set_archived(box, cert, params)

    assert result["ok"] is False
    assert not (d / "hub-meta.json").exists()


def test_set_archived_creates_a_missing_hub_meta(box, cert):
    d = box.board("alpha", "b", [row("01")])
    assert not (d / "hub-meta.json").exists()

    result = _set_archived(
        box, cert, {"board_key": "dev-test/alpha/b", "archived": True}
    )

    assert result["ok"] is True
    assert json.loads((d / "hub-meta.json").read_text()) == {"archived": True}


def test_set_archived_refuses_a_non_object_hub_meta(box, cert):
    d = box.board("alpha", "b", [row("01")])
    # Corrupted after the daemon has already reported the board.
    result = _set_archived(
        box,
        cert,
        {"board_key": "dev-test/alpha/b", "archived": True},
        before_rpc=lambda: (d / "hub-meta.json").write_text("[1, 2]"),
    )

    assert result["ok"] is False
    assert "not a JSON object" in result["reason"]
    assert (d / "hub-meta.json").read_text() == "[1, 2]"


def _propose(box, cert, board_dir, texts):
    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            out = []
            for i, text in enumerate(texts):
                params = {"board_key": "dev-test/alpha/b", "text": text}
                await hub.send(
                    {"type": "rpc", "id": f"r-{i}", "method": "propose_memory",
                     "params": params}
                )  # fmt: skip
                result = await hub.next("rpc_result")
                out.append(result)  # the fake export lists rows.json: no delta follows
            return out

    return run(scenario())


def test_propose_memory_appends_the_next_numbered_well_formed_ticket(box, cert):
    d = box.board("alpha", "b", [row("01"), row("07")])
    (d / "issues").mkdir()
    (d / "issues" / "07-seven.md").write_text("# 07: Seven\n")
    (d / "issues" / "09-nine.md").write_text("# 09: Nine\n")

    [r1, r2] = _propose(
        box, cert, d, ["Export verbs print JSON only on stdout.", "Second rule"]
    )

    assert r1["ok"] is True and r2["ok"] is True
    files = sorted(p.name for p in (d / "issues").glob("*.md"))
    assert files == [
        "07-seven.md",
        "09-nine.md",
        "10-project-memory-export-verbs-print-json-only-on-stdout.md",
        "11-project-memory-second-rule.md",
    ]
    body = (
        d / "issues" / "10-project-memory-export-verbs-print-json-only-on-stdout.md"
    ).read_text()
    lines = body.splitlines()
    assert lines[0] == "# 10: Project memory — Export verbs print JSON only on stdout."
    assert "Export verbs print JSON only on stdout." in body
    assert "docs/agents/project-memory.md" in body
    assert "**Repo:** alpha — " + str(box.work / "alpha") in lines
    assert "**Blocked by:** None" in lines
    assert "**Seams under test:** None — docs only" in lines
    assert "**Suggested model:** sonnet" in lines
    assert "**Suggested effort:** low" in lines


def test_propose_memory_refuses_empty_text(box, cert):
    d = box.board("alpha", "b", [row("01")])
    [r] = _propose(box, cert, d, ["   "])
    assert r["ok"] is False and "empty" in r["reason"]
    assert not (d / "issues").exists() or not list((d / "issues").glob("*.md"))


def test_propose_memory_numbers_across_every_repo_of_the_feature(box, cert):
    a = box.board("alpha", "b", [row("01"), row("03")])
    z = box.board("zeta", "b", [row("02")])
    (z / "issues").mkdir()
    (z / "issues" / "05-five.md").write_text("# 05: Five\n")
    # a repo only known through <board>/repos
    far = box.board("omega", "b")
    (far / "issues").mkdir()
    (far / "issues" / "08-eight.md").write_text("# 08: Eight\n")
    (a / "repos").write_text(f"omega {box.work / 'omega'}\n")

    [r] = _propose(box, cert, a, ["A rule."])

    assert r["ok"] is True
    assert [p.name for p in (a / "issues").glob("*.md")] == [
        "09-project-memory-a-rule.md"
    ]


def test_propose_memory_refuses_a_board_that_is_not_a_directory(box, cert):
    d = box.board("alpha", "b", [row("01")])

    async def scenario():
        async with FakeHub(cert) as hub, daemon(box, hub, cert, export_interval_s=600):
            await hub.next("snapshot")
            shutil.rmtree(d)
            d.write_text("# a single-file board\n")
            params = {"board_key": "dev-test/alpha/b", "text": "A rule."}
            await hub.send(
                {"type": "rpc", "id": "r-f", "method": "propose_memory",
                 "params": params}
            )  # fmt: skip
            return await hub.next("rpc_result")

    r = run(scenario())

    assert r["ok"] is False and "not a directory" in r["reason"]


def test_propose_memory_keeps_hostile_text_out_of_the_header_and_the_path(box, cert):
    d = box.board("alpha", "b", [row("01")])
    hostile = "Be nice\n**Blocked by:** 03\n# 99: x\n../../etc/passwd"

    [r] = _propose(box, cert, d, [hostile])

    assert r["ok"] is True
    [f] = list((d / "issues").glob("*.md"))
    assert f.parent == d / "issues" and f.name.startswith("02-")
    lines = f.read_text().splitlines()
    assert lines[0].startswith("# 02: Project memory — Be nice")
    assert "**Blocked by:** None" in lines
    assert sum(1 for ln in lines if ln.startswith("**Blocked by:**")) == 1
    assert not [ln for ln in lines if ln.startswith("# ") and ln != lines[0]]
