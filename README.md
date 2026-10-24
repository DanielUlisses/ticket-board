# ticket-board (tk-hub)

One self-hosted control plane, reachable over the tailnet, that shows and drives
every ticket-skill board on every client devbox. A daemon on each devbox reports
its boards to the hub; the hub stores and broadcasts them; the web UI shows them
live and sends actions back as RPCs.

## Layout

```
protocol/            the contract between the three parts
  schema.json        JSON Schema (draft 2020-12) of every message; `x-protocol-version`
  fixtures/          realistic messages, each valid against the schema
  fixtures-invalid/  deliberately broken messages, each rejected by the schema
hub-server/          FastAPI hub, stdlib sqlite3, websockets (uv project, Python 3.12+)
devbox-daemon/       tk-export.sh, tk-daemon.py, agy_runner.py; runtime deps: websockets only (uv project)
hub-web/             Vite + TypeScript + React UI; Vitest + Testing Library
  src/styles/tokens.css   design tokens and typography
Makefile             setup, test, lint for every component
```

Only the scaffold exists so far. The daemon, hub and UI code arrive in later tickets.

## Requirements

- [uv](https://docs.astral.sh/uv/). It fetches Python 3.12+ itself when needed.
- Node.js 22.22.2+, 24.15+ or 26+ (what jsdom and Vitest need), with npm.
- `shellcheck`, `make`.

## Running it

From the repo root:

| Command      | What it does |
|--------------|--------------|
| `make setup` | `uv sync` both Python projects, `npm ci` in `hub-web/` |
| `make test`  | pytest in `hub-server/` and `devbox-daemon/`, Vitest in `hub-web/` |
| `make lint`  | shellcheck on every `*.sh`, ruff check + format check on both Python projects, `tsc --noEmit` on `hub-web/` |

`make test` and `make lint` work from a clean clone: `uv run` syncs on first use,
and the web targets run `npm ci` when `node_modules` is missing.

Per component:

- **hub-server**: `cd hub-server && uv run pytest`. Run it with
  `uv run hub-server serve --host-name tk-hub.<tailnet>.ts.net` (or set
  `TK_HUB_HOSTNAMES`, comma-separated): any other `Host` is refused, so without it
  only `localhost`/`127.0.0.1` work. Daemon tokens: `uv run hub-server token add <devbox>`.
- **devbox-daemon**: `cd devbox-daemon && uv run pytest`
- **hub-web**: `cd hub-web && npm ci`, then `npm run dev` (dev server), `npm test`, `npm run lint` or `npm run build` (writes `dist/`, never committed)

## Protocol

`protocol/schema.json` defines the messages, one document per message, with
`type` selecting the shape:

- **daemon → hub** (`/ws/daemon`): `hello` (protocol version, health), `snapshot`
  (`full`, or changed boards plus `removed`), `rpc_result`, and
  `synthesis_started` / `synthesis_finished` / `synthesis_failed`, which the hub
  relays to the UI as they are (for its own box's boards only).
- **hub → daemon**: `welcome`, `refused` (with a reason, e.g. a major
  `protocol_version` mismatch), `rpc` (`id`, `method` from a fixed list, `params`).
- **hub → UI** (`/ws/ui`): `board_synced`, `board_removed`, `ticket_moved`,
  `synthesis_started` / `synthesis_finished` / `synthesis_failed`, `rpc_result`,
  `devbox_status`.
- **hub REST** (`/api`): response bodies are `$defs/rest_boards`, `rest_report`,
  `rest_activity`, `rest_diff`, `rest_scan`, `rest_archive`, `rest_set_policy`, `rest_refine`
  (`POST /api/boards/{key}/tickets/{nn}/comment`, `{comment}`), `rest_set_task_done`
  (`POST …/tickets/{nn}/tasks`, `{text, done}`), and `rest_error` for any error.

A board snapshot (`$defs/board`) is `tk.sh export <board>` (ticket-skill) merged with
`.scratch/project-memory.md` (gitignored; the old `docs/agents/project-memory.md`
is the one-release fallback), `.scratch/<board>/hub-meta.json` and the resolved
`tk.sh policy <board>`. It is keyed `<devbox>/<repo>/<board>`. A ticket
(`$defs/ticket`) is a `board_rows` row plus its hub-meta fields (`summary`,
`reviewer_notes`, `full_report_md`, `manual_tasks`, `synthesis_backend`,
`synthesis_status`, `synthesis_error`, `new_memory_rule`, review state), stored in
hub-meta under `tickets.<NN>`.

`protocol_version` is `MAJOR.MINOR`. The current version is the schema's
`x-protocol-version`. Bump MINOR for additive changes and MAJOR for breaking ones;
the hub refuses a daemon whose major differs.

Both pytest (`*/tests/test_protocol_fixtures.py`) and Vitest
(`hub-web/src/protocol/fixtures.test.ts`) check every file in `protocol/fixtures/`
against the schema, and check that every file in `protocol/fixtures-invalid/` is
rejected. When you change the schema, add or adjust a fixture in the same change.

## Daemon

`devbox-daemon/tk-export.sh` prints every board on the box as one full `snapshot`
(boards under `${TK_SCAN_GLOB:-$HOME/work/*/.scratch/*}` in main checkouts, via
`tk.sh boards` / `tk.sh export`, plus memory, hub-meta, policy, usage and herdr
health). The default glob skips board dirs whose name starts with `.`.
`devbox-daemon/tk-daemon.py` connects to the hub, sends `hello` and a
full snapshot, then re-exports every 20 s and sends only the boards whose hash
changed. It runs the RPCs in its fixed list (`rescan_boards`, `set_archived`,
`set_policy`, `refine_with_comment`, `set_task_done`) and
reconnects with exponential backoff (1 s doubling to 60 s, with jitter).

`~/.config/tk-daemon/config.json`:

```json
{
  "url": "wss://tk-hub.<tailnet>.ts.net/ws/daemon",
  "ip": "100.64.0.10",
  "token_ref": "op://<vault>/<item>/<field>"
}
```

The daemon connects to `ip` and verifies TLS against the name in `url`. The token
comes from `op read <token_ref>` and is kept in memory only. Optional keys:
`devbox` (short hostname), `export_interval_s` (20), `backoff_initial_s` (1),
`backoff_max_s` (60), `rpc_timeout_s` (120), `rpc_synthesis_timeout_s` (600),
`hello_timeout_s` (30), `export_timeout_s` (60: the whole export, a backstop; a
hung export counts as failed), `export_call_timeout_s` (30: each `tk.sh` and
`herdr` call inside `tk-export.sh`, passed as `TK_EXPORT_CALL_TIMEOUT`). The
daemon also passes `TK_EXPORT_DEADLINE`, an epoch a little under
`export_timeout_s` from the start (5 s, or a fifth for short timeouts): no call
runs past it, the boards not exported by then fail, and the snapshot still
arrives. herdr and usage are read before any board, so the deadline never skips
them. A run cut short says where it stopped (`tk-export: resume <i>`) and the
daemon starts the next one there (`TK_EXPORT_START`), so successive runs take
turns and every board is exported (a board that alone outlasts the deadline
starts the next run one place later, so it can't starve the others). If the export before `hello` fails anyway, the daemon still connects,
with the last health it knew (or herdr not ok, usage unknown), and sends the
full snapshot as soon as an export succeeds. `rescan_boards` waits for an export already
running for at most `rpc_timeout_s - export_timeout_s`, then fails with
"export already running". SIGTERM / SIGINT stop the daemon, closing the connection with 1001, and kill
any running export, with everything it started.
The box policy layer, `~/.config/tk-daemon/policy.json`, is read by `tk-export.sh`.

### Synthesis

Card summaries, full reports and manual checklists are written by a cheap CLI
(`devbox-daemon/agy_runner.py`), never by the coding agents. A run starts when a
ticket moves into `needs`, `human` or `pr` between two exports (a ticket or board
seen for the first time has not moved; archived boards are skipped), and on the
`refine_with_comment` RPC (the sheet's ✦ Update via agy, with an optional comment).
One run per ticket at a time.

The context is the ticket file,
`${XDG_STATE_HOME:-~/.local/state}/ticket-skill/<repo>/reports/<branch>.md` when present, `tk.sh show <board> <NN> 180`, `.scratch/project-memory.md` and the
comment, each cut to fit one argv item. The board's resolved policy picks the
backends, tried in order: `synthesis.backends` (default `["agy"]`; each one of
`agy`, `cursor`, `claude`), the next one only when one times out, is missing, exits
non-zero or prints no valid synthesis. Each runs in the repo root with stdin from
`/dev/null` and the prompt as one argument, killed (with everything it started)
after `synthesis.timeout` seconds (default 300; a whole number of seconds, 1 or
more). The schema holds the policy to those ranges, and the daemon checks every
board against it before sending: a board whose `synthesis.timeout` or
`synthesis.backends` is out of range fails the schema and stops syncing (it keeps
its last copy; the daemon logs the schema error) until the policy is fixed.
The runner's own fallbacks, an invalid `synthesis.timeout` giving 300 with a warning
per run and an unknown backend skipped, are defence in depth for a policy that
reaches it without passing the schema, not a tolerance to rely on.
`TK_AGY_TIMEOUT` in the daemon's environment, when set, overrides the policy; it is
read at daemon start, is not checked by the schema, and must be a finite number of
seconds above 0 (fractions allowed), else it is ignored with one warning:

| backend | default command |
|---|---|
| `agy` | `agy -p <prompt> --output-format json` |
| `cursor` | `cursor-agent -p --output-format json --mode ask <prompt>` |
| `claude` | `claude -p --output-format json --model haiku <prompt>` |

Override one in the box policy with `"synthesis": {"commands": {"agy": {"bin":
"agy", "args": ["-p", "{prompt}", "--output-format", "json"]}}}` (`{prompt}` marks
where the prompt goes, else it is appended). A known permission-skip flag in a
backend's args is refused (`--dangerously-skip-permissions`, `--yolo`, `-y`,
`-f`/`--force`, a `--mode` / `--permission-mode` / `--approval-mode` that
auto-approves). The check matches whole arguments only: a wrapper such as
`sh -c '...'` is not looked into. A refused flag, like an unknown backend or a bad
`bin` / `args`, makes that backend misconfigured: when its turn comes it is
skipped with a warning in the daemon log, never run, and the next backend runs. Only
when every backend fails is the run failed, its `synthesis_error` naming each one's
reason, e.g. `agy: refused flag --yolo: it would skip permission prompts; cursor:
timed out after 300s`. The output is one JSON object
`{summary, reviewer_notes, full_report_md, manual_tasks, new_memory_rule}`, taken
from the CLI's JSON envelope (`response`, `result`, `text`, ...), bare, or from a
JSON block in its text. agy's envelope (agy 1.4.6) is one line `{conversation_id,
status, response, duration_seconds, num_turns, usage}`; a `status` other than
`SUCCESS`, like claude's or cursor-agent's `is_error: true`, fails that backend.

The result goes to `tickets.<NN>` in the board's `hub-meta.json`, and the file as it
was before the run to `hub-meta.prev.json`. `full_report_md` is the coordinator
report verbatim, then the backend's reasoning. The checklist merges: developer
items are never removed (a synthesis item with a developer item's text gives way to
it, its tick kept); an item the run repeats (compared by normalised text) keeps its
tick; a synthesis item the run drops goes only if unchecked. While a run
works the ticket's `synthesis_status` is `running`, and its `synthesis_started`
names the first configured backend (the one tried first, not necessarily the one
that answers); a failure keeps the last good content with
`synthesis_status: failed` and `synthesis_error`. A run cut short (the
RPC timed out, the daemon stopped) ends `failed` with `synthesis_error: interrupted`;
one left `running` by a killed daemon is marked so at the next export. The
`set_task_done` RPC writes a tick.

Before sending, the daemon checks the health and every board against
`protocol/schema.json` (the hub drops a whole snapshot that fails it). It does
this with its own small checker for the keywords the schema uses, not jsonschema,
so websockets stays its only runtime dependency; a test keeps the checker in
agreement with jsonschema on every fixture. The schema is read from
`../protocol/schema.json` next to the daemon (config key `schema` overrides it),
so deploy `protocol/` with `devbox-daemon/`.

What fails a board (`tk-export: failed <key> <board dir>` on stderr, in the
daemon log; the daemon keeps sending its last copy rather than reporting it
removed, matching the board dir against the `board_path` it last sent, so a key
guessed wrong in the shell still matches):

- `tk.sh export` errors, times out, or prints no JSON object.
- `tk.sh policy` errors (its stderr goes to the log), times out, or prints no JSON
  object. When `tk.sh help` names no `policy` verb (`tk.sh policy`, or `policy`
  in a verb list; prose like `.scratch/policy.json` does not count), the policy
  layers are merged in `tk-export.sh` instead. When `tk.sh help` itself fails or
  times out, every board fails.
- `hub-meta.json` exists but is not a JSON object, or its `tickets` can't be merged.
- `tk.sh boards` errors, times out, or prints no JSON array of `{path}` objects:
  every board of that repo fails. Relative paths are taken from the repo root.
- The export deadline passes before the board's turn.
- The board fails the schema (a mistyped policy value, say): the daemon logs it.

A failed board the daemon has no copy of (it has failed since the daemon
started) would be missing from a full snapshot, and the hub would remove it. So
until every board exports or has a last copy, the daemon sends deltas instead of
full snapshots (`full snapshot held back` in its log), and `rescan_boards` says
so in its `output_tail`.

A hung `herdr status server` reports `herdr.ok: false`. A `usage.json` that is
empty, unreadable, more than one value, not an object, or has a non-object `rate_limits` reports usage as
`unknown`; percentages are clamped to 0..100.
