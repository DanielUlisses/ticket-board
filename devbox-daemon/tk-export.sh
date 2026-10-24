#!/usr/bin/env bash
# tk-export.sh — every ticket-skill board on this box as one protocol `snapshot`
# message (full: true, protocol/schema.json) on stdout. tk-daemon.py runs it and
# diffs the per-board hashes; it is also runnable by hand.
#
# Boards: every directory matching ${TK_SCAN_GLOB:-$HOME/work/*/.scratch/*}
# whose repo is a main checkout (.git is a directory, not a worktree's file),
# as `tk.sh boards` lists it, exported with `tk.sh export <path>` — both run from
# the main checkout. Each board is merged with:
#   project_memory  <root>/docs/agents/project-memory.md (read only, never written)
#   retro_proposals_md  .scratch/<board>/retro/proposals.md (what `tk.sh retro-save` wrote), or null
#   hub_meta        .scratch/<board>/hub-meta.json; its `tickets.<NN>` objects are
#                   merged into those tickets instead
#   policy          `tk.sh policy <path>` when `tk.sh help` lists that verb (an error
#                   from it fails the board), else the layers defaults
#                   < ~/.config/tk-daemon/policy.json < <root>/.scratch/policy.json
#                   < .scratch/<board>/policy.json
#   hash            sha256 of the board minus exported_at/hash, keys sorted
# and the box health (read first): `herdr status server`, ~/.local/state/tk-daemon/usage.json
# (unreadable or malformed: usage state "unknown"), tickets in working/review,
# daemon version.
#
# A board fails when its export, policy or hub-meta.json is malformed or errors;
# every board of a repo fails when `tk.sh boards` errors or prints no JSON array.
# Each tk.sh and herdr call is killed after TK_EXPORT_CALL_TIMEOUT seconds (30):
# a hung board fails that board, a hung herdr is herdr ok:false. With
# TK_EXPORT_DEADLINE (epoch seconds), no call runs past it: the boards not
# exported by then fail, and the snapshot still prints.
#
# Env: TK_SCAN_GLOB, TK_DEVBOX (default: short hostname), TK_DAEMON_VERSION,
#      TK_SH (default: tk.sh on PATH, else the installed skill's),
#      TK_EXPORT_CALL_TIMEOUT, TK_EXPORT_DEADLINE, TK_EXPORT_START (the index,
#      in board-dir order, to start at).
# stderr: logs (tk.sh's own stderr included), one `tk-export: failed <board_key>
# <board_dir>` line per board whose export failed — it is left out of stdout, and
# the daemon keeps its last copy rather than reporting it removed — and, when the
# deadline cut the run short, `tk-export: resume <index>`.
set -euo pipefail

PROTOCOL_VERSION="1.0"
DEVBOX="${TK_DEVBOX:-$(hostname -s)}"
DAEMON_VERSION="${TK_DAEMON_VERSION:-0.1.0}"
SCAN_GLOB="${TK_SCAN_GLOB:-$HOME/work/*/.scratch/*}"
BOX_POLICY="$HOME/.config/tk-daemon/policy.json"
USAGE_FILE="$HOME/.local/state/tk-daemon/usage.json"
STALE_AFTER=1800   # usage older than this while agents run is stale (board.md, Scheduler)
CALL_TIMEOUT="${TK_EXPORT_CALL_TIMEOUT:-30}"
DEADLINE="${TK_EXPORT_DEADLINE:-}"

if [[ -n "${TK_SH:-}" ]]; then :
elif command -v tk.sh >/dev/null 2>&1; then TK_SH=tk.sh
else TK_SH="$HOME/.claude/skills/implement-tickets/scripts/tk.sh"; fi

log() { printf 'tk-export: %s\n' "$*" >&2; }
command -v jq >/dev/null 2>&1 || { log "jq is not installed"; exit 1; }
[[ "$CALL_TIMEOUT" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { log "TK_EXPORT_CALL_TIMEOUT must be seconds, got $CALL_TIMEOUT"; exit 1; }
[[ -z "$DEADLINE" || "$DEADLINE" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { log "TK_EXPORT_DEADLINE must be epoch seconds, got $DEADLINE"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
NOW="$(date -u +%s)"

# Seconds a call may take: the call timeout, capped at what is left before the
# deadline; 0.000 once it has passed.
budget() {
  if [[ -z "$DEADLINE" ]]; then echo "$CALL_TIMEOUT"; return; fi
  awk -v d="$DEADLINE" -v now="$(date +%s.%N)" -v c="$CALL_TIMEOUT" \
    'BEGIN { r = d - now; if (r > c) r = c; if (r < 0) r = 0; printf "%.3f\n", r }'
}
past_deadline() { [[ -n "$DEADLINE" && "$(budget)" == 0.000 ]]; }

# A command under its budget (`timeout` kills it, and what it started, with
# TERM, then KILL 5 s later). Exit 124: timed out or skipped, said on stderr.
call() {
  local rc=0 t
  t="$(budget)"
  [[ "$t" != 0.000 ]] || { log "$(basename "$1") ${2:-} skipped: export deadline passed"; return 124; }
  timeout -k 5 "$t" "$@" || rc=$?
  (( rc != 124 )) || log "$(basename "$1") ${2:-} timed out after ${t}s"
  return "$rc"
}

# Exactly one JSON object from stdin, compact, or fail.
one_obj() {
  jq -cse 'if length == 1 and (.[0] | type) == "object" then .[0] else error("not one JSON object") end' 2>/dev/null
}

# A JSON object from a file, or {} (missing, or not an object — said on stderr).
read_obj() {
  [[ -f "$1" ]] || { echo '{}'; return; }
  one_obj <"$1" || { log "ignoring $1: not a JSON object"; echo '{}'; }
}

# A JSON object from a file, or {} when missing; fails when it is not one.
read_obj_strict() {
  [[ -f "$1" ]] || { echo '{}'; return; }
  one_obj <"$1" || { log "$1 is not a JSON object"; return 1; }
}

# A file's text as a JSON string, or null.
read_text() {
  if [[ -f "$1" ]]; then jq -Rs . "$1"; else echo null; fi
}

# Whether this tk.sh has the `policy` verb: `tk.sh help` names it as a command
# (`tk.sh policy`, `tk policy`) or in a verb list (`(…, policy, …)`), not in
# prose like `.scratch/policy.json`. Asked once. Returns 0 yes, 1 no, 2 when
# help failed or timed out — then every board fails rather than guessing.
POLICY_VERB=""
policy_verb() {  # <root>
  local help
  if [[ -z "$POLICY_VERB" ]]; then
    if help="$(cd "$1" && call "$TK_SH" help)"; then
      if grep -Eq '(^|[^[:alnum:]_/.-])tk(\.sh)?[[:space:]]+policy([[:space:]]|$)|[(,][[:space:]]*policy[[:space:]]*[,)]' <<<"$help"; then
        POLICY_VERB=yes
      else POLICY_VERB=no; fi
    else
      POLICY_VERB=error
      log "tk.sh help failed: cannot tell whether tk.sh has a policy verb, so no board is exported"
    fi
  fi
  case "$POLICY_VERB" in yes) return 0 ;; no) return 1 ;; *) return 2 ;; esac
}

# The policy for one board: tk.sh's own resolution when it has the verb (any
# error from it fails the board), else the same layers resolved here (forge as
# the export detected it).
board_policy() {  # <root> <board_path> <export_file>
  local root="$1" path="$2" exp="$3" verb=0
  read_obj "$BOX_POLICY" >"$TMP/l-box"
  read_obj "$root/.scratch/policy.json" >"$TMP/l-repo"
  read_obj "$path/policy.json" >"$TMP/l-board"
  policy_verb "$root" || verb=$?
  (( verb != 2 )) || return 1
  if (( verb == 0 )); then
    (cd "$root" && call "$TK_SH" policy "$path") >"$TMP/policy-out" \
      || { log "tk.sh policy $path failed"; return 1; }
    one_obj <"$TMP/policy-out" || { log "tk.sh policy $path printed no JSON object"; return 1; }
    return
  fi
  jq -c --slurpfile box "$TMP/l-box" --slurpfile repo "$TMP/l-repo" --slurpfile board "$TMP/l-board" '
    {forge: .forge, require_human_before_pr: true, auto_merge: false, night_shift: false,
     autonomy: "auto", max_parallel: 5, usage_gate_pct: 90,
     synthesis: {backends: ["agy"], timeout: 300}}
    * $box[0] * $repo[0] * $board[0]' "$exp"
}

export_board() {  # <root> <board_path> -> one board object on stdout
  local root="$1" path="$2" exp="$TMP/export.json" b
  (cd "$root" && call "$TK_SH" export "$path") >"$exp" || return 1
  jq -e 'type == "object"' "$exp" >/dev/null 2>&1 || { log "tk.sh export $path printed no JSON object"; return 1; }
  board_policy "$root" "$path" "$exp" >"$TMP/policy.json" || return 1
  # Which layer each key came from (the layer files board_policy just read).
  jq -c --arg env "${TICKET_FORGE:-}" --argjson remote "$(jq '.has_remote == true' "$exp")" \
        --slurpfile box "$TMP/l-box" --slurpfile repo "$TMP/l-repo" \
        --slurpfile board "$TMP/l-board" '
    . as $p | keys | map(. as $k | {($k):
      (if $k == "forge" and ($remote | not) then "no-remote"
       elif $k == "forge" and $env != "" then "env"
       elif ($board[0] | has($k)) then "board"
       elif ($repo[0] | has($k)) then "repo"
       elif ($box[0] | has($k)) then "box"
       elif $k == "forge" then "detected"
       else "default" end)}) | add' "$TMP/policy.json" >"$TMP/policy-sources.json" || return 1
  read_obj_strict "$path/hub-meta.json" >"$TMP/meta.json" || return 1
  read_text "$root/docs/agents/project-memory.md" >"$TMP/memory.json"
  read_text "$path/retro/proposals.md" >"$TMP/proposals.json"
  b="$(jq -cS --arg devbox "$DEVBOX" --slurpfile policy "$TMP/policy.json" \
        --slurpfile psrc "$TMP/policy-sources.json" \
        --slurpfile meta "$TMP/meta.json" --slurpfile memory "$TMP/memory.json" \
        --slurpfile proposals "$TMP/proposals.json" '
    ($meta[0].tickets // {}) as $mt
    | . + {key: "\($devbox)/\(.repo)/\(.slug)", devbox: $devbox,
           project_memory: $memory[0], retro_proposals_md: $proposals[0],
           hub_meta: ({archived: false, board_brief: null} + ($meta[0] | del(.tickets))),
           policy: $policy[0], policy_sources: $psrc[0],
           tickets: [.tickets[] | {summary: null, reviewer_notes: null, full_report_md: null,
                                   manual_tasks: [], synthesis_backend: null, synthesis_status: "none"}
                                  + . + ($mt[.nn] // {})]}' "$exp")" || return 1
  # A failed step must fail the board, never print nothing (the daemon would report it removed).
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$b" || return 1
  jq -c --arg hash "sha256:$(sha256sum <<<"$b" | cut -d' ' -f1)" \
        --arg now "$(date -u -d "@$NOW" +%Y-%m-%dT%H:%M:%SZ)" \
        '. + {hash: $hash, exported_at: $now}' <<<"$b" || return 1
}

# ---- health (first: running out of time on boards must not skip it) -----------------
if herdr_out="$(call herdr status server 2>&1)" && grep -q '^status: running' <<<"$herdr_out"; then herdr_ok=true
else herdr_ok=false; fi
[[ -n "$herdr_out" ]] || herdr_out="herdr status server printed nothing"

# Exactly one JSON object, else null (empty, several values, not JSON: said on stderr).
usage_in='null'
if [[ -f "$USAGE_FILE" ]]; then
  usage_in="$(jq -cs --argjson mtime "$(stat -c %Y "$USAGE_FILE")" \
               'if length == 1 and (.[0] | type) == "object" then .[0] + {_mtime: $mtime} else null end' \
               "$USAGE_FILE" 2>/dev/null)" || usage_in=''
  [[ -n "$usage_in" && "$usage_in" != null ]] || { log "ignoring $USAGE_FILE: not one JSON object"; usage_in='null'; }
fi

# ---- boards -----------------------------------------------------------------------
declare -A WANT=()   # board dirs matched by the glob
declare -A ROOTS=()  # their repos' main checkouts
while IFS= read -r d; do
  [[ -d "$d" ]] || continue
  d="$(cd "$d" && pwd -P)"
  root="$(dirname "$(dirname "$d")")"
  [[ -d "$root/.git" ]] || continue   # a worktree's .scratch is not where boards live
  WANT["$d"]=1; ROOTS["$root"]=1
done < <(compgen -G "$SCAN_GLOB" || true)

# `tk-export: failed <key> <board dir>`: the key as tk.sh makes it (board-lib.sh:
# `/` -> `-`, leading `.`/`-` stripped; repo: the checkout's name), and the dir,
# which the daemon matches against the board_path it last sent.
fail_board() {  # <root> <real board dir>
  local id="${2#"$1"/.scratch/}" slug
  slug="$(tr '/' '-' <<<"$id" | sed 's/^[.-]*//')"
  echo "tk-export: failed $DEVBOX/$(basename "$1")/$slug $2" >&2
}

# Every board to export, as `<real dir>\t<root>\t<path as listed>`, sorted.
: >"$TMP/todo"
while IFS= read -r root; do
  [[ -n "$root" ]] || continue
  # A failed or malformed list fails every board of the repo: none is reported removed.
  if ! list="$(cd "$root" && call "$TK_SH" boards)" \
     || ! jq -e 'type == "array" and all(.[]; type == "object" and (.path | type) == "string")' \
             >/dev/null 2>&1 <<<"$list"; then
    log "tk.sh boards failed in $root, or printed no JSON array of boards"
    for d in "${!WANT[@]}"; do
      [[ "$d" == "$root/.scratch/"* ]] && fail_board "$root" "$d"
    done
    continue
  fi
  while IFS= read -r path; do
    [[ "$path" == /* ]] || path="$root/$path"
    real="$(cd "$path" 2>/dev/null && pwd -P)" || continue
    [[ -n "${WANT[$real]:-}" ]] || continue   # tk.sh lists it, but the glob doesn't match it
    printf '%s\t%s\t%s\n' "$real" "$root" "$path" >>"$TMP/todo"
  done < <(jq -r '.[].path' <<<"$list")
done < <(printf '%s\n' "${!ROOTS[@]}" | sort)
mapfile -t TODO < <(sort -u "$TMP/todo")

# Start at TK_EXPORT_START (mod the count), so that runs cut short by the deadline
# take turns: the first board not exported is said as `tk-export: resume <i>`,
# for the daemon to pass back — the one after it when the deadline cut the very
# board the run started with, so one slow board can't take every run's window.
START="${TK_EXPORT_START:-0}"
[[ "$START" =~ ^[0-9]+$ ]] || { log "ignoring TK_EXPORT_START=$START: not an index"; START=0; }
START=$((10#$START))
: >"$TMP/boards.jsonl"
resume=""
for (( n = 0; n < ${#TODO[@]}; n++ )); do
  i=$(( (START + n) % ${#TODO[@]} ))
  IFS=$'\t' read -r real root path <<<"${TODO[$i]}"
  if past_deadline; then   # out of time: the daemon keeps its last copy
    [[ -n "$resume" ]] || resume="$i"
    fail_board "$root" "$real"
    continue
  fi
  if ! export_board "$root" "$path" >>"$TMP/boards.jsonl"; then
    log "board $path not exported"
    fail_board "$root" "$real"
    # Cut short by the deadline: this board goes first next time, unless it was
    # first this time too.
    if [[ -z "$resume" ]] && past_deadline; then
      if (( n == 0 )); then resume=$(( (i + 1) % ${#TODO[@]} )); else resume="$i"; fi
    fi
  fi
done
[[ -z "$resume" ]] || echo "tk-export: resume $resume" >&2

jq -s -c --arg pv "$PROTOCOL_VERSION" --arg devbox "$DEVBOX" --arg version "$DAEMON_VERSION" \
      --argjson herdr_ok "$herdr_ok" --arg herdr "$herdr_out" --argjson usage "$usage_in" \
      --argjson now "$NOW" --argjson stale_after "$STALE_AFTER" '
  # A timestamp (epoch seconds or RFC 3339) as epoch seconds, or null.
  def epoch: if type == "number" then floor
             elif type == "string" then (try (sub("\\.[0-9]+"; "") | fromdateiso8601) catch null)
             else null end;
  # Epoch seconds as a schema timestamp, or null (out of range).
  def ts: try (todate | if test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$") then . else null end)
          catch null;
  # One rate-limit window, its percentage clamped to 0..100; an expired one is 0 %
  # (board.md, Scheduler).
  def window: if type != "object" then null else
                (.resets_at | epoch) as $r | .used_percentage as $u
                | if $r == null or ($u | type) != "number" or ($r | ts) == null then null
                  else {used_percentage: (if $r <= $now then 0 else ([0, ([100, $u] | min)] | max) end),
                        resets_at: ($r | ts)} end
              end;
  {state: "unknown", five_hour: null, seven_day: null, updated_at: null} as $unknown
  | ([.[].tickets[] | select(.col == "working" or .col == "review")] | length) as $running
  # Malformed usage is "unknown", never a failed export.
  | (if $usage == null then $unknown
     else try (
       ($usage.rate_limits | if . == null then $usage elif type == "object" then . else null end) as $rl
       | if $rl == null then $unknown else
           (($usage.updated_at | epoch) // $usage._mtime) as $at
           | {five_hour: ($rl.five_hour | window), seven_day: ($rl.seven_day | window), updated_at: ($at | ts)}
           | if .five_hour == null and .seven_day == null then $unknown
             else .state = (if $running > 0 and $now - $at > $stale_after then "stale" else "ok" end) end
         end) catch $unknown
     end) as $usage_out
  | {type: "snapshot", protocol_version: $pv, devbox: $devbox, full: true,
     health: {daemon_version: $version, herdr: {ok: $herdr_ok, status: $herdr},
              usage: $usage_out, running: $running, last_sync: null},
     boards: sort_by(.key), removed: []}' "$TMP/boards.jsonl"
