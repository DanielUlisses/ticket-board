"""The hub's SQLite (stdlib sqlite3): a rebuildable cache of board snapshots plus
hub-only data (token hashes, devbox registry, activity log). board.md, Source of
truth."""

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    devbox     TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devboxes (
    name             TEXT PRIMARY KEY,
    online           INTEGER NOT NULL DEFAULT 0,
    last_seen        TEXT,
    health           TEXT,
    protocol_version TEXT,
    refused_reason   TEXT
);
CREATE TABLE IF NOT EXISTS boards (
    key    TEXT PRIMARY KEY,
    devbox TEXT NOT NULL,
    board  TEXT NOT NULL,
    stale  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS boards_devbox ON boards (devbox);
CREATE TABLE IF NOT EXISTS activity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    board_key TEXT NOT NULL,
    nn        TEXT NOT NULL,
    from_col  TEXT NOT NULL,
    to_col    TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_ticket ON activity (board_key, nn);
"""


def now() -> str:
    """RFC 3339 UTC with a `Z` suffix, as the protocol's `timestamp` requires."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_token(token: str) -> str:
    # Tokens are 256-bit random strings, so a plain SHA-256 is enough: there is
    # nothing to brute-force that a slow KDF would protect.
    return hashlib.sha256(token.encode()).hexdigest()


class TokenExists(Exception):
    pass


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection, used only from the event loop thread (async handlers).
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.boards = Boards(self.db)

    def close(self):
        self.db.close()

    # -- tokens ------------------------------------------------------------

    def add_token(self, devbox: str) -> str:
        """Create a token for `devbox`, store its hash, return the token (once)."""
        token = secrets.token_urlsafe(32)
        try:
            self.db.execute(
                "INSERT INTO tokens (devbox, token_hash, created_at) VALUES (?, ?, ?)",
                (devbox, hash_token(token), now()),
            )
        except sqlite3.IntegrityError:
            raise TokenExists(devbox) from None
        return token

    def revoke_token(self, devbox: str) -> bool:
        return (
            self.db.execute("DELETE FROM tokens WHERE devbox = ?", (devbox,)).rowcount
            > 0
        )

    def check_token(self, devbox: str, token: str) -> bool:
        row = self.db.execute(
            "SELECT token_hash FROM tokens WHERE devbox = ?", (devbox,)
        ).fetchone()
        # Compare even when the devbox is unknown, so timing says nothing.
        stored = row["token_hash"] if row else hash_token(secrets.token_urlsafe(32))
        return hmac.compare_digest(stored, hash_token(token)) and row is not None

    # -- devbox registry -----------------------------------------------------

    def update_devbox(self, name: str, **fields):
        """Upsert registry fields: online, last_seen, health, protocol_version,
        refused_reason."""
        if "health" in fields:
            fields["health"] = json.dumps(fields["health"])
        if "online" in fields:
            fields["online"] = int(fields["online"])
        self.db.execute("INSERT OR IGNORE INTO devboxes (name) VALUES (?)", (name,))
        if fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            self.db.execute(
                f"UPDATE devboxes SET {assignments} WHERE name = ?",
                (*fields.values(), name),
            )

    def devbox_status(self, name: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM devboxes WHERE name = ?", (name,)
        ).fetchone()
        return _devbox_status(row)

    def devbox_statuses(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM devboxes ORDER BY name").fetchall()
        return [_devbox_status(row) for row in rows]


def _devbox_status(row: sqlite3.Row) -> dict:
    """A registry row as a `devbox_status` message."""
    status = {
        "type": "devbox_status",
        "devbox": row["name"],
        "online": bool(row["online"]),
        "last_seen": row["last_seen"],
        "health": json.loads(row["health"]) if row["health"] else None,
    }
    if row["protocol_version"]:
        status["protocol_version"] = row["protocol_version"]
    if row["refused_reason"]:
        status["refused_reason"] = row["refused_reason"]
    return status


class Boards:
    """Board snapshots and the activity log, on the same connection as Store."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT b.board, b.stale, d.last_seen FROM boards b"
            " LEFT JOIN devboxes d ON d.name = b.devbox WHERE b.key = ?",
            (key,),
        ).fetchone()
        return _board(row) if row else None

    def all(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT b.board, b.stale, d.last_seen FROM boards b"
            " LEFT JOIN devboxes d ON d.name = b.devbox ORDER BY b.key"
        ).fetchall()
        return [_board(row) for row in rows]

    def keys(self, devbox: str) -> list[str]:
        rows = self.db.execute(
            "SELECT key FROM boards WHERE devbox = ? ORDER BY key", (devbox,)
        ).fetchall()
        return [row["key"] for row in rows]

    def put(self, board: dict):
        board = {k: v for k, v in board.items() if k not in ("stale", "last_seen")}
        self.db.execute(
            "INSERT INTO boards (key, devbox, board, stale) VALUES (?, ?, ?, 0)"
            " ON CONFLICT (key) DO UPDATE SET board = excluded.board, stale = 0",
            (board["key"], board["devbox"], json.dumps(board)),
        )

    def remove(self, key: str) -> bool:
        return self.db.execute("DELETE FROM boards WHERE key = ?", (key,)).rowcount > 0

    def mark_stale(self, devbox: str | None = None):
        """Mark one devbox's boards stale, or every board when devbox is None."""
        if devbox is None:
            self.db.execute("UPDATE boards SET stale = 1")
        else:
            self.db.execute("UPDATE boards SET stale = 1 WHERE devbox = ?", (devbox,))

    def log_move(self, board_key: str, nn: str, from_col: str, to_col: str, at: str):
        self.db.execute(
            "INSERT INTO activity (board_key, nn, from_col, to_col, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (board_key, nn, from_col, to_col, at),
        )

    def activity(self, board_key: str, nn: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM activity WHERE board_key = ? AND nn = ? ORDER BY id",
            (board_key, nn),
        ).fetchall()
        return [_moved(row) for row in rows]


def _board(row: sqlite3.Row) -> dict:
    board = json.loads(row["board"])
    board["stale"] = bool(row["stale"])
    if row["last_seen"]:
        board["last_seen"] = row["last_seen"]
    return board


def _moved(row: sqlite3.Row) -> dict:
    """An activity row as a `ticket_moved` message."""
    return {
        "type": "ticket_moved",
        "board_key": row["board_key"],
        "nn": row["nn"],
        "from": row["from_col"],
        "to": row["to_col"],
        "at": row["at"],
    }
