"""`hub-server` command line: `serve`, `token add|revoke <devbox>`."""

import argparse
import os
import sys
from pathlib import Path

from hub_server import NAME
from hub_server.store import Store, TokenExists

DEFAULT_DB = Path.home() / ".local/share/tk-hub/hub.db"


def default_db() -> Path:
    return Path(os.environ.get("TK_HUB_DB") or DEFAULT_DB)


def devbox_name(value: str) -> str:
    if not NAME.match(value):
        raise argparse.ArgumentTypeError(f"not a devbox name: {value!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub-server")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the hub")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", type=Path, default=None, help="default: $TK_HUB_DB")
    serve.add_argument(
        "--allow-login",
        action="append",
        default=None,
        help="Tailscale-User-Login to admit (repeatable); default: comma-separated "
        "$TK_HUB_ALLOWED_LOGINS; none set admits the whole tailnet",
    )
    serve.add_argument(
        "--host-name",
        action="append",
        default=None,
        help="the hub's own name or IP accepted as Host (repeatable), e.g. "
        "tk-hub.<tailnet>.ts.net; default: comma-separated $TK_HUB_HOSTNAMES. "
        "localhost and 127.0.0.1 are always accepted",
    )
    serve.add_argument(
        "--web-dist",
        type=Path,
        default=None,
        help="default: $TK_HUB_WEB_DIST or hub-web/dist",
    )

    token = sub.add_parser("token", help="manage per-devbox daemon tokens")
    token_sub = token.add_subparsers(dest="action", required=True)
    for action, text in (
        ("add", "create a token; prints it once, stores only its hash"),
        ("revoke", "delete the token; the daemon is refused on its next connect"),
    ):
        p = token_sub.add_parser(action, help=text)
        p.add_argument("devbox", type=devbox_name)
        p.add_argument("--db", type=Path, default=None, help="default: $TK_HUB_DB")

    args = parser.parse_args(argv)
    db = args.db or default_db()

    if args.command == "serve":
        import uvicorn

        from hub_server.app import DEFAULT_WEB_DIST, create_app

        env_logins = os.environ.get("TK_HUB_ALLOWED_LOGINS", "")
        logins = args.allow_login or [
            x.strip() for x in env_logins.split(",") if x.strip()
        ]
        web_dist = (
            args.web_dist or os.environ.get("TK_HUB_WEB_DIST") or DEFAULT_WEB_DIST
        )
        env_hosts = os.environ.get("TK_HUB_HOSTNAMES", "")
        hosts = args.host_name or [x.strip() for x in env_hosts.split(",") if x.strip()]
        app = create_app(
            db_path=db, allowed_logins=logins, web_dist=web_dist, host_names=hosts
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    store = Store(db)
    try:
        if args.action == "add":
            try:
                print(store.add_token(args.devbox))
            except TokenExists:
                print(
                    f"hub-server: {args.devbox} already has a token; revoke it first",
                    file=sys.stderr,
                )
                return 1
            return 0
        if not store.revoke_token(args.devbox):
            print(f"hub-server: {args.devbox} has no token", file=sys.stderr)
            return 1
        print(f"revoked {args.devbox}", file=sys.stderr)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
