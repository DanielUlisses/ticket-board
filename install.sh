#!/usr/bin/env bash
# install.sh: installs tk-hub on this machine, in one of two roles. Running it
# again changes nothing unless the checkout, the arguments or the files it
# writes have changed; a service is restarted only when what it runs differs
# from what it last ran (a stamp written once the restart succeeded).
#
#   install.sh daemon [--url <hub url>] [--ip <hub tailnet ip>] [--token-ref <op://…>]
#       On a devbox, as the box user: a uv venv in ~/.local/share/tk-hub/daemon-venv,
#       ~/.config/tk-daemon/config.json (url, ip, token_ref set from the arguments,
#       every other key kept; an argument left out keeps the stored value), a seeded
#       ~/.config/tk-daemon/policy.json (never overwritten), tk-statusline wired into
#       ~/.claude/settings.json, and the tk-daemon systemd --user unit, with
#       linger enabled through sudo so it runs without a login.
#
#   install.sh hub [--host-name <tk-hub.<tailnet>.ts.net>]
#       On the hub LXC, as the user the hub runs as (root or one with sudo): a uv
#       venv in ~/.local/share/tk-hub/hub-venv, hub-web built into hub-web/dist
#       (node through mise when it is installed), the SQLite database in
#       /var/lib/tk-hub, the tk-hub system unit on 127.0.0.1:8000, and
#       `tailscale serve` publishing it over HTTPS on 443. The host name defaults
#       to the one in the existing unit, else this node's MagicDNS name. Prints
#       how to mint a daemon token.
#
# Without systemd (no /run/systemd/system) the units are written but nothing is
# enabled or started.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DATA="$HOME/.local/share/tk-hub"
PORT=8000

# devbox-daemon
DAEMON_VENV="$DATA/daemon-venv"
DAEMON_CONF_DIR="$HOME/.config/tk-daemon"
DAEMON_CONFIG="$DAEMON_CONF_DIR/config.json"
DAEMON_POLICY="$DAEMON_CONF_DIR/policy.json"
DAEMON_UNIT=tk-daemon.service
DAEMON_UNIT_FILE="$HOME/.config/systemd/user/$DAEMON_UNIT"
# The box policy layer (board.md, Policy / Scheduler): caps and synthesis defaults.
DAEMON_POLICY_SEED='{
  "max_parallel": 5,
  "usage_gate_pct": 90,
  "synthesis": {
    "backends": ["agy"],
    "timeout": 300
  }
}'

# hub
HUB_VENV="$DATA/hub-venv"
HUB_STATE=/var/lib/tk-hub
HUB_DB="$HUB_STATE/hub.db"
HUB_UNIT=tk-hub.service
HUB_UNIT_FILE="/etc/systemd/system/$HUB_UNIT"

usage() {
  cat <<'EOF'
usage: install.sh daemon [--url <hub url>] [--ip <hub tailnet ip>] [--token-ref <op://…>]
       install.sh hub [--host-name <tk-hub.<tailnet>.ts.net>]
EOF
}

die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

say() {
  printf 'install.sh: %s\n' "$*" >&2
}

need() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null || die "$c not found on PATH"
  done
}

# Runs a command as root: as is when already root, else through sudo.
as_root() {
  if ((EUID == 0)); then "$@"; else sudo "$@"; fi
}

# Writes $2 to file $1 (mode $3) when its content differs; returns 1 when
# unchanged (a failed write dies). With a 4th argument `root`, writes it as root.
write_if_changed() {
  local file="$1" content="$2" mode="$3" who="${4:-}"
  [[ -f $file && $(<"$file") == "$content" ]] && return 1
  if [[ $who == root ]]; then
    as_root install -D -m "$mode" /dev/stdin "$file" <<<"$content" || die "cannot write $file"
  else
    install -D -m "$mode" /dev/stdin "$file" <<<"$content" || die "cannot write $file"
  fi
}

# A hash of the given strings: what a service is about to run.
content_hash() {
  printf '%s\0' "$@" | sha256sum | cut -d' ' -f1
}

# true when stamp file $1 does not hold hash $2, i.e. the service has not yet
# been (re)started on what it should run now.
needs_restart() {
  if [[ -f $1 && $(<"$1") == "$2" ]]; then echo false; else echo true; fi
}

# A hash of the files under the given paths (relative to the checkout), skipping
# tests, build output, dependencies and caches: what a service runs.
tree_hash() {
  (
    cd "$ROOT"
    find "$@" \( -name node_modules -o -name dist -o -name .venv -o -name tests \
      -o -name __pycache__ -o -name '.*_cache' \) -prune -o -type f -print0 |
      sort -z | xargs -0 sha256sum
  ) | sha256sum | cut -d' ' -f1
}

# Syncs the uv project $1 (locked, no dev dependencies) into venv $2.
uv_venv() {
  local project="$1" venv="$2"
  UV_PROJECT_ENVIRONMENT="$venv" uv sync --locked --no-dev --quiet --project "$ROOT/$project"
  say "$project: venv $venv in sync"
}

have_systemd() {
  [[ -d /run/systemd/system ]]
}

# Unit $2 in scope $1 (user: systemctl --user; system: as root): reloads and
# restarts it when $3 is true, then enables and starts it if it isn't. Any
# failing step exits (set -e), so the caller writes its stamp only on success.
ensure_unit() {
  local scope="$1" unit="$2" restart="$3"
  local -a ctl=(as_root systemctl) query=(systemctl)
  if [[ $scope == user ]]; then
    ctl=(systemctl --user)
    query=(systemctl --user)
  fi
  if $restart; then
    "${ctl[@]}" daemon-reload
    "${ctl[@]}" try-restart "$unit"
  fi
  if [[ $("${query[@]}" is-enabled "$unit" 2>/dev/null) != enabled ]] ||
    ! "${query[@]}" is-active -q "$unit"; then
    "${ctl[@]}" enable --now "$unit"
    say "$unit: enabled and started"
  elif $restart; then
    say "$unit: restarted"
  else
    say "$unit: running"
  fi
}

# ---- daemon -----------------------------------------------------------------

# config.json from the stored one (if any) and the arguments; returns 1 when unchanged.
daemon_config() {
  local url="$1" ip="$2" ref="$3" cur='{}' new missing
  if [[ -f $DAEMON_CONFIG ]]; then
    cur="$(jq -e 'if type == "object" then . else error("not a JSON object") end' "$DAEMON_CONFIG" 2>/dev/null)" ||
      die "$DAEMON_CONFIG is not a JSON object; fix or remove it"
  fi
  new="$(jq --arg url "$url" --arg ip "$ip" --arg ref "$ref" \
    '. + ({url: $url, ip: $ip, token_ref: $ref} | with_entries(select(.value != "")))' <<<"$cur")"
  missing="$(jq -r '[("url", "ip", "token_ref") as $k | select((.[$k] // "") == "") | $k] | join(", ")' <<<"$new")"
  [[ -z $missing ]] || die "daemon: no $missing yet; pass --url, --ip and --token-ref"
  [[ -f $DAEMON_CONFIG && $(jq -S . <<<"$cur") == $(jq -S . <<<"$new") ]] && return 1
  write_if_changed "$DAEMON_CONFIG" "$new" 644
}

daemon_statusline() {
  local settings="$HOME/.claude/settings.json" before after
  before="$(cat "$settings" 2>/dev/null || true)"
  if ! "$DAEMON_VENV/bin/python" "$ROOT/devbox-daemon/tk-statusline-install"; then
    say "statusline: not wired (see above)"
    return
  fi
  after="$(cat "$settings" 2>/dev/null || true)"
  if [[ $before == "$after" ]]; then
    say "statusline: tk-statusline already wired"
  else
    say "statusline: wired tk-statusline into $settings"
  fi
}

# Waits up to 10 s for the user's systemd manager (just started by linger). The
# environment may be someone else's (devboxes runs this through runuser from
# root), so the runtime dir is always this user's and no session bus is reused.
user_manager() {
  XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export XDG_RUNTIME_DIR
  unset DBUS_SESSION_BUS_ADDRESS
  for _ in {1..20}; do
    systemctl --user show-environment >/dev/null 2>&1 && return
    sleep 0.5
  done
  die "daemon: no systemd --user manager for $(id -un) (XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR)"
}

daemon() {
  local url="" ip="" ref="" unit applied restart user
  while (($#)); do
    case "$1" in
      --url) url="${2:-}"; shift 2 || die "--url needs a value" ;;
      --ip) ip="${2:-}"; shift 2 || die "--ip needs a value" ;;
      --token-ref) ref="${2:-}"; shift 2 || die "--token-ref needs a value" ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "daemon: unknown argument $1" ;;
    esac
  done
  [[ -z $url || $url =~ ^(wss|https):// ]] || die "--url must be wss:// or https://, got $url"
  [[ -z $ip || $ip =~ ^[0-9A-Fa-f:.]+$ ]] || die "--ip must be an IP address, got $ip"
  [[ -z $ref || $ref == op://* ]] || die "--token-ref must be an op:// reference"
  ((EUID != 0)) || die "daemon: run it as the box user, not root"
  need uv jq
  user="$(id -un)"

  uv_venv devbox-daemon "$DAEMON_VENV"

  if daemon_config "$url" "$ip" "$ref"; then
    say "daemon: wrote $DAEMON_CONFIG"
  else
    say "daemon: $DAEMON_CONFIG up to date"
  fi
  if [[ -e $DAEMON_POLICY ]]; then
    say "daemon: $DAEMON_POLICY kept"
  else
    write_if_changed "$DAEMON_POLICY" "$DAEMON_POLICY_SEED" 644
    say "daemon: seeded $DAEMON_POLICY"
  fi

  daemon_statusline

  # The token is read by `op read` with the box's 1Password service-account
  # token, loaded the way the box's .bashrc does; never written anywhere.
  unit="[Unit]
Description=tk-daemon: reports this devbox's ticket boards to the tk-hub

[Service]
Environment=PATH=%h/.local/bin:%h/.local/share/mise/shims:/usr/local/bin:/usr/bin
ExecStart=/bin/bash -c 'f=\"\$\$HOME/.config/op/service-account-token\"; if [ -s \"\$\$f\" ]; then OP_SERVICE_ACCOUNT_TOKEN=\"\$\$(cat \"\$\$f\")\"; export OP_SERVICE_ACCOUNT_TOKEN; fi; exec \"\$\$@\"' tk-daemon \"$DAEMON_VENV/bin/python\" \"$ROOT/devbox-daemon/tk-daemon.py\"
Restart=on-failure
RestartSec=30
TimeoutStopSec=90

[Install]
WantedBy=default.target"
  if write_if_changed "$DAEMON_UNIT_FILE" "$unit" 644; then
    say "daemon: wrote $DAEMON_UNIT_FILE"
  fi
  # What it runs: its unit, its config and the code (a new checkout restarts it).
  applied="$(content_hash "$unit" "$(<"$DAEMON_CONFIG")" "$(tree_hash devbox-daemon protocol)")"
  restart="$(needs_restart "$DATA/daemon.stamp" "$applied")"

  if ! have_systemd; then
    say "daemon: no systemd, not started"
    return
  fi
  if [[ $(loginctl show-user "$user" -p Linger --value 2>/dev/null) != yes ]]; then
    as_root loginctl enable-linger "$user"
    say "daemon: enabled linger for $user"
  fi
  user_manager
  ensure_unit user "$DAEMON_UNIT" "$restart"
  write_if_changed "$DATA/daemon.stamp" "$applied" 644 || true
}

# ---- hub --------------------------------------------------------------------

# Runs node tooling: through mise's node LTS when mise is installed, else the
# node on PATH.
with_node() {
  if command -v mise >/dev/null; then
    mise exec node@lts -- "$@"
  else
    need node npm
    "$@"
  fi
}

# hub-web/dist, rebuilt only when hub-web's sources changed since the last build.
hub_web() {
  local stamp
  stamp="$(tree_hash hub-web)"
  if [[ -f $ROOT/hub-web/dist/index.html && -f $DATA/web.stamp && $(<"$DATA/web.stamp") == "$stamp" ]]; then
    say "hub-web: dist up to date"
    return
  fi
  say "hub-web: building dist"
  (cd "$ROOT/hub-web" && with_node npm ci --no-fund --no-audit --loglevel=error && with_node npm run build --silent) >&2
  write_if_changed "$DATA/web.stamp" "$stamp" 644 || true
  say "hub-web: built $ROOT/hub-web/dist"
}

# The proxy `tailscale serve` has for / on https://$1:443, or nothing.
served_proxy() {
  as_root tailscale serve status --json 2>/dev/null |
    jq -r --arg k "$1:443" '.Web[$k].Handlers["/"].Proxy? // empty'
}

hub() {
  local host_name="" unit applied restart user group target cur ip
  while (($#)); do
    case "$1" in
      --host-name) host_name="${2:-}"; shift 2 || die "--host-name needs a value" ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "hub: unknown argument $1" ;;
    esac
  done
  need uv jq tailscale
  user="$(id -un)"
  group="$(id -gn)"
  # Left out: the one the installed unit already serves, else MagicDNS.
  if [[ -z $host_name && -f $HUB_UNIT_FILE ]]; then
    host_name="$(sed -n 's/.* --host-name "\([^"]*\)".*/\1/p' "$HUB_UNIT_FILE" | head -n1)"
  fi
  if [[ -z $host_name ]]; then
    host_name="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty')"
    host_name="${host_name%.}"
    [[ -n $host_name ]] || die "hub: no MagicDNS name from tailscale; pass --host-name"
  fi
  [[ $host_name =~ ^[A-Za-z0-9.-]+$ ]] || die "hub: --host-name must be a DNS name, got $host_name"

  uv_venv hub-server "$HUB_VENV"
  hub_web

  if [[ $(stat -c %U:%G "$HUB_STATE" 2>/dev/null) != "$user:$group" ]]; then
    as_root install -d -o "$user" -g "$group" -m 750 "$HUB_STATE"
    say "hub: made $HUB_STATE ($user:$group)"
  fi

  unit="[Unit]
Description=tk-hub: the ticket-board control plane, behind tailscale serve
Wants=network-online.target
After=network-online.target tailscaled.service

[Service]
User=$user
Group=$group
ExecStart=\"$HUB_VENV/bin/hub-server\" serve --host 127.0.0.1 --port $PORT --db \"$HUB_DB\" --web-dist \"$ROOT/hub-web/dist\" --host-name \"$host_name\"
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target"
  if write_if_changed "$HUB_UNIT_FILE" "$unit" 644 root; then
    say "hub: wrote $HUB_UNIT_FILE"
  fi
  # What it runs: its unit and the code (dist is read from disk on each
  # request: no restart).
  applied="$(content_hash "$unit" "$(tree_hash hub-server protocol)")"
  restart="$(needs_restart "$DATA/hub.stamp" "$applied")"

  if have_systemd; then
    ensure_unit system "$HUB_UNIT" "$restart"
    write_if_changed "$DATA/hub.stamp" "$applied" 644 || true
  else
    say "hub: no systemd, not started"
  fi

  # `tailscale serve` is applied only when its 443 proxy is not this hub: never
  # reset. Its output goes to stderr: it may ask to enable Serve/HTTPS at a URL.
  target="http://127.0.0.1:$PORT"
  cur="$(served_proxy "$host_name")"
  if [[ $cur == "$target" ]]; then
    say "hub: tailscale serve already publishes https://$host_name -> $target"
  else
    [[ -z $cur ]] || say "hub: replacing tailscale serve https://$host_name -> $cur"
    as_root tailscale serve --bg --https=443 "$target" >&2
    say "hub: tailscale serve publishes https://$host_name -> $target"
  fi

  ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  cat <<EOF

Hub: https://$host_name (unit $HUB_UNIT, database $HUB_DB)
Mint a token for a devbox (printed once; the hub keeps only its hash):
  TK_HUB_DB=$HUB_DB $HUB_VENV/bin/hub-server token add <devbox>
Store it in 1Password, then on the devbox:
  install.sh daemon --url wss://$host_name/ws/daemon --ip ${ip:-<the hub tailnet IP>} --token-ref op://<vault>/<item>/<field>
EOF
}

case "${1:-}" in
  daemon) shift; daemon "$@" ;;
  hub) shift; hub "$@" ;;
  -h | --help) usage ;;
  *) usage >&2; exit 2 ;;
esac
