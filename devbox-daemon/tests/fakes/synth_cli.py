#!/usr/bin/env python3
"""Fake synthesis CLI, installed as `agy`, `cursor-agent` and `claude`. Each call is
appended to $FAKE_SYNTH_LOG as {"bin", "argv", "cwd", "stdin"} (stdin: what reading
it gave, "" for /dev/null). The mode comes from $FAKE_SYNTH_MODE_<BIN> (bin upper-cased,
`-` as `_`), else $FAKE_SYNTH_MODE, else `good`:
  good     the CLI's JSON envelope holding the payload as JSON text: agy 1.4.6's
           {conversation_id, status: "SUCCESS", response, ...}, the others'
           {type: "result", is_error, result, ...}
  text     the envelope holding prose with the payload in a ```json block
  raw      the payload itself, bare JSON on stdout
  garbage  not JSON at all
  invalid  JSON in the envelope, but not a synthesis (no summary)
  is_error the envelope with is_error: true
  status_error  agy's envelope with status "ERROR" (exit 0)
  agy_error  partial output, `AGY_ERROR:` on stderr, exit 3
  slow     sleeps $FAKE_SYNTH_DELAY seconds (1), then prints the good envelope
  hang     sleeps $FAKE_SYNTH_HANG seconds (60), printing nothing
  linger   starts a helper (its own process group, same session) that keeps stdout
           and stderr open for 60 s, writes its pid to $FAKE_SYNTH_PIDFILE, then
           prints the good envelope and exits 0
The payload: $FAKE_SYNTH_PAYLOAD (JSON), else a good one naming the bin (a board
brief's, when the prompt asks for the BOARD BRIEF)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
stdin = "" if sys.stdin is None or sys.stdin.closed else sys.stdin.read()
if log := os.environ.get("FAKE_SYNTH_LOG"):
    with open(log, "a") as f:
        f.write(
            json.dumps(
                {"bin": name, "argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin}
            )
            + "\n"
        )

mode = (
    os.environ.get(f"FAKE_SYNTH_MODE_{name.upper().replace('-', '_')}")
    or os.environ.get("FAKE_SYNTH_MODE")
    or "good"
)
payload = (
    json.loads(os.environ["FAKE_SYNTH_PAYLOAD"])
    if os.environ.get("FAKE_SYNTH_PAYLOAD")
    else {
        "landed": [f"Export landed ({name})"],
        "in_flight": ["02 is in review"],
        "needs_you": ["01 waits for you"],
        "next": ["Launch 03"],
    }
    if any("BOARD BRIEF" in a for a in sys.argv[1:])
    else {
        "summary": f"Written by {name}.\nSecond line.",
        "reviewer_notes": "Check the edge case.",
        "full_report_md": f"## Reasoning\n\n{name} thinks it is fine.",
        "manual_tasks": ["Open the sheet on a phone", "Tick a box"],
        "new_memory_rule": None,
    }
)


def envelope(text: str, is_error: bool = False, status: str = "SUCCESS") -> str:
    if name == "agy":
        env = {"conversation_id": "c-1", "status": status, "response": text + "\n",
               "duration_seconds": 1.5, "num_turns": 1,
               "usage": {"input_tokens": 10, "output_tokens": 5}}  # fmt: skip
        return json.dumps(env | ({"is_error": True} if is_error else {}))
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": is_error,
         "result": text, "session_id": "s-1"}
    )  # fmt: skip


if mode == "good":
    print(envelope(json.dumps(payload)))
elif mode == "text":
    print(
        envelope(
            f"Here you go:\n\n```json\n{json.dumps(payload, indent=2)}\n```\nDone."
        )
    )
elif mode == "raw":
    print(json.dumps(payload))
elif mode == "garbage":
    print("I could not do that, sorry. {not json")
elif mode == "invalid":
    print(envelope(json.dumps({"answer": 42})))
elif mode == "is_error":
    print(envelope(json.dumps(payload), is_error=True))
elif mode == "status_error":
    print(envelope(json.dumps(payload), status="ERROR"))
elif mode == "agy_error":
    print('{"response": "partial')
    print("AGY_ERROR: quota exhausted", file=sys.stderr)
    sys.exit(3)
elif mode == "linger":
    helper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
    )
    Path(os.environ["FAKE_SYNTH_PIDFILE"]).write_text(str(helper.pid))
    print(envelope(json.dumps(payload)), flush=True)
elif mode == "slow":
    time.sleep(float(os.environ.get("FAKE_SYNTH_DELAY", "1")))
    print(envelope(json.dumps(payload)))
elif mode == "hang":
    time.sleep(float(os.environ.get("FAKE_SYNTH_HANG", "60")))
else:
    sys.exit(f"fake synth: unknown mode {mode}")
