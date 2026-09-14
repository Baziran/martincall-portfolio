#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_URL="${MARTINCALL_URL:-http://127.0.0.1:8000}"
APP_SERVICE="martincall-app"
DB_SERVICE="martincall-timescaledb"
TICK_SERVICE="martincall-tick-feed"
CODEX_BRIDGE_SCRIPT="scripts/codex_advisor_bridge.py"
CODEX_BRIDGE_TOKEN_FILE="secrets/codex_advisor_bridge.token"
CODEX_BRIDGE_RUNTIME_DIR="${PROJECT_DIR}/../data/codex-advisor/runtime"
CODEX_BRIDGE_PID_FILE="${CODEX_BRIDGE_RUNTIME_DIR}/bridge.pid"
CODEX_BRIDGE_LOG_FILE="${CODEX_BRIDGE_RUNTIME_DIR}/bridge.log"
DISCORD_COMPANION_MODULE="aef_terminal.indicators.modules.discord_signals.companion"
DISCORD_COMPANION_ENV_FILE="secrets/discord_signals_companion.env"
DISCORD_COMPANION_PID_FILE="${PROJECT_DIR}/../data/discord-signals/runtime/companion.pid"
DISCORD_COMPANION_LOG_FILE="${PROJECT_DIR}/../data/discord-signals/runtime/companion.log"
REMOTE_SSH_HOST="${MARTINCALL_REMOTE_SSH_HOST:-bazserv-remote}"
REMOTE_LOCAL_HOST="${MARTINCALL_REMOTE_LOCAL_HOST:-127.0.0.1}"
REMOTE_LOCAL_PORT="${MARTINCALL_REMOTE_LOCAL_PORT:-18000}"
REMOTE_TARGET_HOST="${MARTINCALL_REMOTE_TARGET_HOST:-127.0.0.1}"
REMOTE_TARGET_PORT="${MARTINCALL_REMOTE_TARGET_PORT:-8000}"
REMOTE_FORWARD_SPEC="${REMOTE_LOCAL_HOST}:${REMOTE_LOCAL_PORT}:${REMOTE_TARGET_HOST}:${REMOTE_TARGET_PORT}"
REMOTE_APP_URL="http://${REMOTE_LOCAL_HOST}:${REMOTE_LOCAL_PORT}"
REMOTE_TUNNEL_RUNTIME_DIR="${PROJECT_DIR}/../data/remote-access/runtime"
REMOTE_TUNNEL_PID_FILE="${REMOTE_TUNNEL_RUNTIME_DIR}/ssh-tunnel.pid"
REMOTE_TUNNEL_LOG_FILE="${REMOTE_TUNNEL_RUNTIME_DIR}/ssh-tunnel.log"
PROJECT_PYTHON="../../venv/bin/python"

# usage:
#./martincall-server.sh start
#./martincall-server.sh stop
#./martincall-server.sh restart
#./martincall-server.sh status
#./martincall-server.sh logs
#./martincall-server.sh sleep
#./martincall-server.sh wake
#./martincall-server.sh codex-start
#./martincall-server.sh codex-stop
#./martincall-server.sh codex-status
#./martincall-server.sh codex-logs
#./martincall-server.sh discord-start
#./martincall-server.sh discord-stop
#./martincall-server.sh discord-status
#./martincall-server.sh discord-logs
#./martincall-server.sh remote-start
#./martincall-server.sh remote-stop
#./martincall-server.sh remote-status
#./martincall-server.sh remote-logs
#./martincall-server.sh research-status
#./martincall-server.sh research-export
#./martincall-server.sh tick-feed

cd "$PROJECT_DIR"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose is not installed."
  exit 1
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") <command>

Commands:
  start       Build if needed and start MartinCall containers
  stop        Stop MartinCall app and database containers
  restart     Restart MartinCall containers
  status      Show container and API status
  logs        Follow app logs
  sleep       Put backend into sleep mode and disconnect IBKR sessions
  wake        Wake backend and allow IBKR polling again
  codex-start Start the host-side ChatGPT-authenticated Codex advisor bridge
  codex-stop  Stop the Codex advisor bridge
  codex-status
              Show Codex advisor bridge process status
  codex-logs  Follow Codex advisor bridge logs
  discord-start
              Start the host-side Discord Desktop RPC companion
  discord-stop
              Stop the Discord Desktop RPC companion
  discord-status
              Show Discord Desktop RPC companion process status
  discord-logs
              Follow Discord Desktop RPC companion logs
  remote-start
              Start the nettop SSH tunnel and Discord Desktop RPC companion
  remote-stop Stop the Discord companion and nettop SSH tunnel
  remote-status
              Show nettop tunnel, remote API, and Discord companion status
  remote-logs Follow nettop SSH tunnel and Discord companion logs
  research-status
              Show autonomous prospective research capture status
  research-export
              Snapshot research journals into the external data export directory
  tick-feed   Run the exclusive offline IBKR tick collector; app must be stopped

Dashboard: $APP_URL/
Remote dashboard: $REMOTE_APP_URL/ via $REMOTE_SSH_HOST
EOF
}

load_discord_companion_environment() {
  if [[ ! -s "$DISCORD_COMPANION_ENV_FILE" ]]; then
    return 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$DISCORD_COMPANION_ENV_FILE"
  set +a
}

ensure_docker() {
  local allow_start="${1:-false}"
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$allow_start" == "true" ]] && command -v colima >/dev/null 2>&1; then
    echo "Docker daemon is not running; starting Colima..."
    colima start
    if docker info >/dev/null 2>&1; then
      return 0
    fi
  fi
  echo "Docker daemon is not running."
  exit 1
}

validate_compose_environment() {
  if ! "${COMPOSE[@]}" config >/dev/null; then
    echo "Compose environment is invalid. Set MARTINCALL_POSTGRES_PASSWORD in the environment or .env."
    exit 1
  fi
}

api_json() {
  local path="$1"
  curl -fsS --max-time 5 "$APP_URL$path"
}

api_post() {
  local path="$1"
  curl -fsS --max-time 8 \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{}" \
    "$APP_URL$path"
}

wait_for_api() {
  local attempt
  for attempt in {1..60}; do
    if api_json "/api/system" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Backend did not become ready at $APP_URL within 60 seconds."
  return 1
}

print_api_status() {
  "${COMPOSE[@]}" exec -T "$APP_SERVICE" python - "$APP_URL" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1].rstrip("/")
try:
    with urllib.request.urlopen(base + "/api/system", timeout=5) as response:
        data = json.load(response)
except Exception as exc:
    print(f"API: unavailable ({exc})")
    raise SystemExit(1)

sleep = data.get("sleep") or {}
ibkr = data.get("ibkr") or {}
storage = data.get("storage") or {}
print(f"API: {data.get('status')}")
print(f"Sleep: {bool(sleep.get('sleeping'))}")
print(
    "IBKR: "
    f"quote={bool(ibkr.get('quote_connected'))} "
    f"chart={bool(ibkr.get('chart_connected'))} "
    f"history={bool(ibkr.get('history_connected'))} "
    f"subscriptions={int(ibkr.get('quote_subscriptions') or 0)}"
)
print(f"Database rows: {int(storage.get('bars') or 0):,}")
if sleep.get("sleeping"):
    print("Server is sleeping. Use './martincall-server.sh wake' or the Wake button before live work.")
PY
}

start_server() {
  ensure_docker true
  validate_compose_environment
  local running_tick
  running_tick="$("${COMPOSE[@]}" ps --status running --services "$TICK_SERVICE")"
  if [[ -n "$running_tick" ]]; then
    echo "The exclusive tick collector is running. Stop it before starting MartinCall."
    exit 1
  fi
  load_discord_companion_environment || true
  echo "Starting MartinCall..."
  export BUILD_GIT_SHA="$(git rev-parse --short HEAD)"
  if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    export BUILD_GIT_SHA="${BUILD_GIT_SHA}-dirty"
  fi
  export BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  "${COMPOSE[@]}" --profile runtime up -d --build "$APP_SERVICE"
  wait_for_api
  echo "Dashboard: $APP_URL/"
  print_api_status
  if [[ -s "$DISCORD_COMPANION_ENV_FILE" ]]; then
    start_discord_companion
  fi
}

run_offline_tick_feed() {
  ensure_docker false
  validate_compose_environment
  local running_app
  running_app="$("${COMPOSE[@]}" ps --status running --services "$APP_SERVICE")"
  if [[ -n "$running_app" ]]; then
    echo "MartinCall app is running. Stop it before starting the exclusive tick collector."
    exit 1
  fi
  echo "Starting exclusive offline IBKR tick collector..."
  "${COMPOSE[@]}" --profile ticks run --rm "$TICK_SERVICE"
}

stop_server() {
  stop_discord_companion
  if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is not running; MartinCall containers are not active."
    return 0
  fi
  validate_compose_environment
  echo "Stopping MartinCall containers..."
  "${COMPOSE[@]}" stop "$APP_SERVICE" "$DB_SERVICE"
  echo "Stopped. Data volume is preserved."
}

sidecar_pid() {
  local pid_file="$1"
  local process_marker="$2"
  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi
  local pid
  pid="$(<"$pid_file")"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" >/dev/null 2>&1; then
    return 1
  fi
  if ! ps -p "$pid" -o command= | grep -F "$process_marker" >/dev/null 2>&1; then
    return 1
  fi
  printf '%s\n' "$pid"
}

codex_bridge_pid() {
  sidecar_pid "$CODEX_BRIDGE_PID_FILE" "$CODEX_BRIDGE_SCRIPT"
}

discord_companion_pid() {
  sidecar_pid "$DISCORD_COMPANION_PID_FILE" "$DISCORD_COMPANION_MODULE"
}

remote_tunnel_pid() {
  sidecar_pid "$REMOTE_TUNNEL_PID_FILE" "$REMOTE_FORWARD_SPEC"
}

start_codex_bridge() {
  local running_pid
  if running_pid="$(codex_bridge_pid)"; then
    echo "Codex advisor bridge is already running (pid=$running_pid)."
    return 0
  fi
  if [[ ! -x "$PROJECT_PYTHON" ]]; then
    echo "Project Python is unavailable at $PROJECT_PYTHON."
    return 1
  fi
  if [[ ! -s "$CODEX_BRIDGE_TOKEN_FILE" ]] || [[ "$(wc -c <"$CODEX_BRIDGE_TOKEN_FILE" | tr -d ' ')" -lt 32 ]]; then
    echo "Create a random capability token in $CODEX_BRIDGE_TOKEN_FILE first."
    return 1
  fi
  mkdir -p "$(dirname "$CODEX_BRIDGE_PID_FILE")"
  umask 077
  echo "Starting Codex advisor bridge..."
  PYTHONPATH=src CODEX_ADVISOR_BRIDGE_TOKEN_FILE="$CODEX_BRIDGE_TOKEN_FILE" \
    nohup "$PROJECT_PYTHON" "$CODEX_BRIDGE_SCRIPT" >"$CODEX_BRIDGE_LOG_FILE" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$CODEX_BRIDGE_PID_FILE"
  local attempt
  for attempt in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      echo "Codex advisor bridge failed to start."
      tail -n 20 "$CODEX_BRIDGE_LOG_FILE" || true
      return 1
    fi
    if grep -F "Codex advisor bridge ready" "$CODEX_BRIDGE_LOG_FILE" >/dev/null 2>&1; then
      echo "Codex advisor bridge is ready (pid=$pid)."
      return 0
    fi
    sleep 0.25
  done
  echo "Codex advisor bridge is running but readiness was not confirmed; inspect $CODEX_BRIDGE_LOG_FILE."
}

stop_codex_bridge() {
  local pid
  if ! pid="$(codex_bridge_pid)"; then
    echo "Codex advisor bridge is not running."
    return 0
  fi
  kill "$pid"
  local attempt
  for attempt in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$CODEX_BRIDGE_PID_FILE"
      echo "Codex advisor bridge stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Codex advisor bridge did not stop within 5 seconds (pid=$pid)."
  return 1
}

status_codex_bridge() {
  local pid
  if pid="$(codex_bridge_pid)"; then
    echo "Codex advisor bridge: ready (pid=$pid, log=$CODEX_BRIDGE_LOG_FILE)"
    return 0
  fi
  echo "Codex advisor bridge: stopped"
  return 1
}

start_discord_companion() {
  local running_pid
  if running_pid="$(discord_companion_pid)"; then
    echo "Discord Signals companion is already running (pid=$running_pid)."
    return 0
  fi
  if [[ ! -x "$PROJECT_PYTHON" ]]; then
    echo "Project Python is unavailable at $PROJECT_PYTHON."
    return 1
  fi
  if ! load_discord_companion_environment; then
    echo "Create $DISCORD_COMPANION_ENV_FILE from its .example file first."
    return 1
  fi
  mkdir -p "$(dirname "$DISCORD_COMPANION_PID_FILE")"
  umask 077
  echo "Starting Discord Signals companion..."
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
    nohup "$PROJECT_PYTHON" -m "$DISCORD_COMPANION_MODULE" \
    >"$DISCORD_COMPANION_LOG_FILE" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$DISCORD_COMPANION_PID_FILE"
  local attempt
  for attempt in {1..80}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      echo "Discord Signals companion failed to start."
      tail -n 20 "$DISCORD_COMPANION_LOG_FILE" || true
      return 1
    fi
    if grep -E "Discord Signals (live|dormant)" \
      "$DISCORD_COMPANION_LOG_FILE" >/dev/null 2>&1; then
      echo "Discord Signals companion is ready (pid=$pid)."
      return 0
    fi
    sleep 0.25
  done
  echo "Discord Signals companion is running; inspect $DISCORD_COMPANION_LOG_FILE for gate state."
}

stop_discord_companion() {
  local pid
  if ! pid="$(discord_companion_pid)"; then
    echo "Discord Signals companion is not running."
    return 0
  fi
  kill "$pid"
  local attempt
  for attempt in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$DISCORD_COMPANION_PID_FILE"
      echo "Discord Signals companion stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Discord Signals companion did not stop within 5 seconds (pid=$pid)."
  return 1
}

status_discord_companion() {
  local pid
  if pid="$(discord_companion_pid)"; then
    echo "Discord Signals companion: running (pid=$pid, log=$DISCORD_COMPANION_LOG_FILE)"
    return 0
  fi
  echo "Discord Signals companion: stopped"
  return 1
}

start_remote_tunnel() {
  local running_pid
  if running_pid="$(remote_tunnel_pid)"; then
    echo "Nettop SSH tunnel is already running (pid=$running_pid, dashboard=$REMOTE_APP_URL/)."
    return 0
  fi
  if ! command -v ssh >/dev/null 2>&1; then
    echo "OpenSSH client is not installed."
    return 1
  fi
  if curl -fsS --max-time 2 "$REMOTE_APP_URL/api/ready" >/dev/null 2>&1; then
    echo "$REMOTE_APP_URL is already served by a process not owned by this launcher."
    echo "Stop the existing manual tunnel before running remote-start."
    return 1
  fi
  mkdir -p "$REMOTE_TUNNEL_RUNTIME_DIR"
  umask 077
  echo "Starting nettop SSH tunnel: $REMOTE_FORWARD_SPEC via $REMOTE_SSH_HOST..."
  nohup ssh \
    -N \
    -T \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L "$REMOTE_FORWARD_SPEC" \
    "$REMOTE_SSH_HOST" \
    >"$REMOTE_TUNNEL_LOG_FILE" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$REMOTE_TUNNEL_PID_FILE"
  local attempt
  for attempt in {1..80}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$REMOTE_TUNNEL_PID_FILE"
      echo "Nettop SSH tunnel failed to start."
      tail -n 20 "$REMOTE_TUNNEL_LOG_FILE" || true
      return 1
    fi
    if curl -fsS --max-time 2 "$REMOTE_APP_URL/api/ready" >/dev/null 2>&1; then
      echo "Nettop SSH tunnel is ready (pid=$pid, dashboard=$REMOTE_APP_URL/)."
      return 0
    fi
    sleep 0.25
  done
  kill "$pid" >/dev/null 2>&1 || true
  rm -f "$REMOTE_TUNNEL_PID_FILE"
  echo "SSH connected, but the remote MartinCall API did not become ready at $REMOTE_APP_URL."
  tail -n 20 "$REMOTE_TUNNEL_LOG_FILE" || true
  return 1
}

stop_remote_tunnel() {
  local pid
  if ! pid="$(remote_tunnel_pid)"; then
    rm -f "$REMOTE_TUNNEL_PID_FILE"
    echo "Nettop SSH tunnel is not running."
    return 0
  fi
  kill "$pid"
  local attempt
  for attempt in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$REMOTE_TUNNEL_PID_FILE"
      echo "Nettop SSH tunnel stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Nettop SSH tunnel did not stop within 5 seconds (pid=$pid)."
  return 1
}

status_remote_tunnel() {
  local pid
  if pid="$(remote_tunnel_pid)"; then
    echo "Nettop SSH tunnel: running (pid=$pid, log=$REMOTE_TUNNEL_LOG_FILE)"
    if curl -fsS --max-time 5 "$REMOTE_APP_URL/api/ready" >/dev/null 2>&1; then
      echo "Remote MartinCall API: ready ($REMOTE_APP_URL/)"
      return 0
    fi
    echo "Remote MartinCall API: unavailable through the running tunnel"
    return 1
  fi
  echo "Nettop SSH tunnel: stopped"
  return 1
}

validate_remote_discord_target() {
  if ! load_discord_companion_environment; then
    echo "Create $DISCORD_COMPANION_ENV_FILE from its .example file first."
    return 1
  fi
  if [[ "${AEF_DISCORD_SIGNALS_TERMINAL_URL:-}" != "$REMOTE_APP_URL" ]]; then
    echo "$DISCORD_COMPANION_ENV_FILE must set AEF_DISCORD_SIGNALS_TERMINAL_URL=$REMOTE_APP_URL"
    return 1
  fi
}

start_remote_access() {
  start_remote_tunnel
  validate_remote_discord_target
  if ! start_discord_companion; then
    echo "The SSH tunnel remains active for browser tabs; inspect the Discord companion log."
    return 1
  fi
  echo "Remote access is ready: $REMOTE_APP_URL/"
  echo "Keep this terminal open; press Ctrl-C to stop the tunnel and companion."
  trap 'trap - INT TERM EXIT; stop_remote_access; exit 0' INT TERM
  trap 'trap - INT TERM EXIT; stop_remote_access' EXIT
  while remote_tunnel_pid >/dev/null 2>&1 && discord_companion_pid >/dev/null 2>&1; do
    sleep 2
  done
  echo "Remote access ended because one of its managed processes stopped."
  return 1
}

stop_remote_access() {
  local status=0
  stop_discord_companion || status=$?
  stop_remote_tunnel || status=$?
  return "$status"
}

status_remote_access() {
  local status=0
  status_remote_tunnel || status=$?
  status_discord_companion || status=$?
  return "$status"
}

status_research_capture() {
  ensure_docker false
  validate_compose_environment
  "${COMPOSE[@]}" exec -T "$APP_SERVICE" python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/system?details=false", timeout=5) as response:
    payload = json.load(response)
with urllib.request.urlopen("http://127.0.0.1:8000/api/system/memory", timeout=5) as response:
    memory = json.load(response)

status = payload.get("research_capture") or {}
print(f"Enabled: {bool(status.get('enabled'))}")
print(f"Configured: {bool(status.get('configured'))}")
print(f"Timeframe: {status.get('timeframe') or '-'}")
print(f"Instruments: {', '.join(status.get('instrument_ids') or ()) or '-'}")
print(f"Active leases: {len(status.get('active_leases') or ())}")
print(f"Registrations: {int(status.get('registrations') or 0)}")
print(f"Renewals: {int(status.get('renewals') or 0)}")
print(f"Failures: {int(status.get('failures') or 0)}")
print(f"Last success: {status.get('last_success_at') or '-'}")
worker = (((memory.get("caches") or {}).get("market_analysis") or {}).get("research_worker") or {})
print(f"Journal writes completed: {int(worker.get('completed') or 0)}")
print(f"Journal writes failed: {int(worker.get('failed') or 0)}")
print(f"Journal writes superseded: {int(worker.get('superseded') or 0)}")
errors = status.get("errors") or {}
if errors:
    print("Errors: " + json.dumps(errors, ensure_ascii=False, sort_keys=True))
PY
}

export_research_capture() {
  ensure_docker false
  validate_compose_environment
  local running_app
  running_app="$("${COMPOSE[@]}" ps --status running --services "$APP_SERVICE")"
  if [[ -z "$running_app" ]]; then
    echo "MartinCall app must be running to export its mounted research data."
    return 1
  fi
  "${COMPOSE[@]}" exec -T "$APP_SERVICE" \
    python scripts/export_research_capture.py
}

case "${1:-}" in
  start)
    start_server
    ;;
  stop)
    stop_server
    ;;
  restart)
    stop_server
    start_server
    ;;
  status)
    ensure_docker false
    validate_compose_environment
    "${COMPOSE[@]}" ps
    print_api_status || true
    status_discord_companion || true
    ;;
  logs)
    ensure_docker false
    validate_compose_environment
    "${COMPOSE[@]}" logs -f "$APP_SERVICE"
    ;;
  sleep)
    ensure_docker false
    validate_compose_environment
    wait_for_api
    api_post "/api/system/sleep" >/dev/null
    print_api_status
    ;;
  wake)
    ensure_docker false
    validate_compose_environment
    wait_for_api
    api_post "/api/system/wake" >/dev/null
    print_api_status
    ;;
  codex-start)
    start_codex_bridge
    ;;
  codex-stop)
    stop_codex_bridge
    ;;
  codex-status)
    status_codex_bridge
    ;;
  codex-logs)
    tail -f "$CODEX_BRIDGE_LOG_FILE"
    ;;
  discord-start)
    start_discord_companion
    ;;
  discord-stop)
    stop_discord_companion
    ;;
  discord-status)
    status_discord_companion
    ;;
  discord-logs)
    tail -f "$DISCORD_COMPANION_LOG_FILE"
    ;;
  remote-start)
    start_remote_access
    ;;
  remote-stop)
    stop_remote_access
    ;;
  remote-status)
    status_remote_access
    ;;
  remote-logs)
    tail -f "$REMOTE_TUNNEL_LOG_FILE" "$DISCORD_COMPANION_LOG_FILE"
    ;;
  research-status)
    status_research_capture
    ;;
  research-export)
    export_research_capture
    ;;
  tick-feed)
    run_offline_tick_feed
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: $1"
    usage
    exit 2
    ;;
esac
