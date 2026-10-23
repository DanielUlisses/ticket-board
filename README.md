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
  (`full`, or changed boards plus `removed`), `rpc_result`.
- **hub → daemon**: `welcome`, `refused` (with a reason, e.g. a major
  `protocol_version` mismatch), `rpc` (`id`, `method` from a fixed list, `params`).
- **hub → UI** (`/ws/ui`): `board_synced`, `board_removed`, `ticket_moved`,
  `synthesis_started` / `synthesis_finished` / `synthesis_failed`, `rpc_result`,
  `devbox_status`.
- **hub REST** (`/api`): response bodies are `$defs/rest_boards`, `rest_report`,
  `rest_activity`, `rest_scan`, and `rest_error` for any error.

A board snapshot (`$defs/board`) is `tk.sh export <board>` (ticket-skill) merged with
`docs/agents/project-memory.md`, `.scratch/<board>/hub-meta.json` and the resolved
`tk.sh policy <board>`. It is keyed `<devbox>/<repo>/<board>`. A ticket
(`$defs/ticket`) is a `board_rows` row plus its hub-meta fields (`summary`,
`reviewer_notes`, `full_report_md`, `manual_tasks`, `synthesis_backend`,
`synthesis_status`, review state).

`protocol_version` is `MAJOR.MINOR`. The current version is the schema's
`x-protocol-version`. Bump MINOR for additive changes and MAJOR for breaking ones;
the hub refuses a daemon whose major differs.

Both pytest (`*/tests/test_protocol_fixtures.py`) and Vitest
(`hub-web/src/protocol/fixtures.test.ts`) check every file in `protocol/fixtures/`
against the schema, and check that every file in `protocol/fixtures-invalid/` is
rejected. When you change the schema, add or adjust a fixture in the same change.
