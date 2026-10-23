"""Shared test scaffolding: a fake box (HOME, ~/work repos with boards) and fake
`tk.sh`, `herdr`, `op` and the synthesis CLIs `agy`, `cursor-agent`, `claude` on
PATH (tests/fakes), and the fake hub's TLS certificate."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

DAEMON_DIR = Path(__file__).resolve().parents[1]
PROTOCOL = DAEMON_DIR.parent / "protocol"
VALIDATOR = Draft202012Validator(json.loads((PROTOCOL / "schema.json").read_text()))
FAKES = Path(__file__).resolve().parent / "fakes"
NAME = "hub.test"  # the hub's MagicDNS name, as in the test certificate


def schema_errors(msg: dict) -> list[str]:
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in VALIDATOR.iter_errors(msg)
    ]


def row(nn: str, col: str = "backlog", **extra) -> dict:
    """One `board_rows` row as tk.sh export prints it (no hub-meta fields)."""
    return {
        "nn": nn, "title": f"Ticket {nn}", "ref": f"{nn}-t.md", "file": f"/x/{nn}-t.md",
        "status": "open", "branch": "", "base": "", "model": "", "effort": "",
        "account": "", "agent": "", "blocked_raw": "None", "blockers": [],
        "smodel": "sonnet", "seffort": "low", "parent": "", "worktree": "", "repo": "",
        "state": "-", "landed": "-", "phase": "-", "round": "", "tab": "-", "pr": None,
        "checks": {}, "open_blockers": [], "col": col, "note": "", "hint": "",
        **extra,
    }  # fmt: skip


class Box:
    """A devbox in tmp: HOME with ~/work/<repo> main checkouts and their boards."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.work = self.home / "work"
        self.bin = tmp / "bin"
        self.tk_log = tmp / "tk.log"
        self.synth_log = tmp / "synth.log"
        self.bin.mkdir(parents=True)
        self.work.mkdir(parents=True)
        for src, name in [
            ("tk_sh.py", "tk.sh"),
            ("herdr.py", "herdr"),
            ("op.py", "op"),
            ("systemd_run.py", "systemd-run"),
            ("devbox.py", "devbox"),
            ("systemctl.py", "systemctl"),
            ("synth_cli.py", "agy"),
            ("synth_cli.py", "cursor-agent"),
            ("synth_cli.py", "claude"),
        ]:
            dst = self.bin / name
            shutil.copy(FAKES / src, dst)
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TK_DEVBOX": "dev-test",
            "FAKE_TK_LOG": str(self.tk_log),
            "FAKE_SYNTH_LOG": str(self.synth_log),
            "FAKE_SYSTEMD_LOG": str(tmp / "systemd.log"),
        }
        self.env.pop("TK_SCAN_GLOB", None)
        self.env.pop("XDG_STATE_HOME", None)

    def repo(self, name: str, worktree: bool = False) -> Path:
        root = self.work / name
        root.mkdir(exist_ok=True)
        git = root / ".git"
        if worktree:
            git.write_text("gitdir: /elsewhere\n")
        else:
            git.mkdir(exist_ok=True)
        return root

    def board(self, repo: str, slug: str, rows=(), files=None) -> Path:
        """files: name -> dict (written as JSON) or str, relative to the board."""
        d = self.repo(repo) / ".scratch" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "rows.json").write_text(json.dumps(list(rows)))
        for name, content in (files or {}).items():
            text = content if isinstance(content, str) else json.dumps(content)
            (d / name).write_text(text)
        return d

    def write(self, rel: str, content) -> Path:
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
        return p

    def synth_calls(self) -> list[dict]:
        if not self.synth_log.exists():
            return []
        return [json.loads(line) for line in self.synth_log.read_text().splitlines()]

    def tk_calls(self) -> list[dict]:
        if not self.tk_log.exists():
            return []
        return [json.loads(line) for line in self.tk_log.read_text().splitlines()]


@pytest.fixture
def box(tmp_path) -> Box:
    return Box(tmp_path)


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
