#!/usr/bin/env bash
# One-command dev launcher: Postgres (Docker) -> supervisor (dashboard_api) ->
# dashboard UI (Vite). Ctrl+C stops the UI, any agent the supervisor started,
# and the supervisor itself; Postgres keeps running since it's a persistent
# service, not something to restart every time.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Deliberately pinned here, NOT read from .env: .env's defaults are tuned for
# deploy.sh/systemd (see deploy/README.md "Đổi port") to avoid colliding with
# other services on a shared server. This script's own vite.config.ts proxy
# target and these values must always match each other, regardless of
# whatever .env says on this machine - hot-reload dev is a separate, fixed
# local setup, not something you'd ever need to move to avoid a collision.
export POSTGRES_PORT=5432
export SUPERVISOR_HOST=127.0.0.1
export SUPERVISOR_PORT=8080
export AGENT_DATABASE_URL="postgresql://agent:agent@127.0.0.1:${POSTGRES_PORT}/agent"

SUPERVISOR_LOG="data/logs/supervisor.log"
mkdir -p data/logs

echo "==> Postgres (docker compose)"
docker compose up -d --wait postgres

echo "==> Supervisor API (dashboard_api) -> $SUPERVISOR_LOG"
uv run python -m dashboard_api > "$SUPERVISOR_LOG" 2>&1 &
SUPERVISOR_PID=$!

cleanup() {
    echo
    # Agent processes are started detached from the supervisor on purpose
    # (start_new_session=True in dashboard_api/processes.py) so a supervisor
    # crash never kills mid-conversation work - but that also means killing
    # the supervisor alone leaves any running agent behind, orphaned and
    # untracked by the next `dev.sh` run. Ask the supervisor to stop each
    # running account gracefully first, through the same API the dashboard's
    # own "Stop Agent" button uses, before taking the supervisor down too.
    echo "==> Stopping any agents the supervisor started"
    curl -fsS http://127.0.0.1:8080/api/accounts 2>/dev/null \
        | python3 -c '
import json, sys
try:
    accounts = json.load(sys.stdin)
except Exception:
    accounts = []
for account in accounts:
    if account.get("processes", {}).get("agent", {}).get("running"):
        print(account["id"])
' 2>/dev/null \
        | while IFS= read -r account_id; do
            echo "    stopping $account_id"
            curl -fsS -X POST "http://127.0.0.1:8080/api/accounts/$account_id/stop" \
                -H "Content-Type: application/json" -d '{"include_chrome": false}' \
                -o /dev/null || true
        done

    echo "==> Stopping supervisor (pid $SUPERVISOR_PID)"
    kill "$SUPERVISOR_PID" 2>/dev/null || true
    wait "$SUPERVISOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> Waiting for supervisor on :8080"
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null http://127.0.0.1:8080/api/accounts 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    echo "!! Supervisor exited early - check $SUPERVISOR_LOG"
    exit 1
fi

echo "==> Dashboard UI (Vite) - opening browser"
cd dashboard
if [ ! -d node_modules ]; then
    echo "==> Installing dashboard dependencies (first run)"
    npm install
fi
npm run dev -- --open
