#!/usr/bin/env bash
# One-command deploy: Postgres + dashboard UI run in Docker (built, static,
# no hot reload); the supervisor (dashboard_api) runs here in the foreground
# because it is the process that spawns Chrome and the agent on this host -
# they need a real, visible window for login/CAPTCHA/OTP, which no container
# can give them. For active frontend development with hot reload, use
# ./dev.sh instead.
#
# Linux only: the dashboard container uses `network_mode: host` (see
# compose.yaml) to reach the supervisor on 127.0.0.1:${SUPERVISOR_PORT:-28080},
# which has no equivalent on Docker Desktop for Mac/Windows.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# `docker compose` auto-loads .env for its own variable substitution, but a
# plain bash/python process (this script, and dashboard_api below) does
# not get it for free - source it here so DASHBOARD_PORT/SUPERVISOR_PORT/
# AGENT_DATABASE_URL/GEMINI_API_KEY/DASHBOARD_SECRET_KEY etc. actually reach
# this shell and the supervisor process, matching what systemd's own
# `EnvironmentFile=.env` already does for the production path.
# Checked before sourcing, never after: a bad line is executed by `source`
# (see deploy/check_env.sh), and under systemd it otherwise shows up only as
# a bare `command not found` looping every 5 seconds behind a 502.
if [ -f .env ]; then
    ./deploy/check_env.sh .env
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

echo "==> Postgres + dashboard UI (docker compose)"
docker compose up -d --build --wait postgres dashboard

DASHBOARD_PORT="${DASHBOARD_PORT:-25173}"
echo "==> Dashboard: http://127.0.0.1:${DASHBOARD_PORT}"
echo "==> Supervisor API (dashboard_api) - Ctrl+C to stop"
# `exec`, so the supervisor IS this unit's main process: the unit runs with
# KillMode=process (deploy/install_systemd_service.sh) so that stopping it
# leaves every workspace's agent and Chrome alive, and under that mode
# systemd signals only the main process. Without `exec` that is this bash,
# which dies without passing SIGTERM on - leaving the old supervisor holding
# :28080 and the next start failing to bind. uv forwards the signal to python.
exec uv run python -m dashboard_api
