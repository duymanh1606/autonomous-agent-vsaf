#!/usr/bin/env bash
# Update a deployed system to the code on this branch, without cutting a
# customer off mid-conversation.
#
# Why this exists as a script: the order matters and one step is easy to
# forget. Agent processes are deliberately detached from the supervisor (see
# deploy/README.md), so `systemctl restart autonomous-agent` picks up new
# supervisor and frontend code while every workspace keeps answering
# customers with the OLD agent code - silently, with nothing in the UI saying
# so. That is the failure this script is for.
#
# What it does, in order:
#   1. back up the database (pg_dump, consistent snapshot, safe while live)
#   2. show what is about to change, and stop unless the tree is clean
#   3. drain every running workspace: stop taking new work, wait for the task
#      in flight to finish, then shut the agent down
#   4. restart the supervisor (this also rebuilds the dashboard frontend)
#   5. print the exact workspaces to Start again
#
# Step 5 is deliberately not automated: starting a workspace goes through the
# supervisor's authenticated API, and putting dashboard credentials in a
# deploy script is a decision for whoever operates this system, not a default.
# Everything up to that point needs no credentials - a drain and a shutdown go
# to each agent's own loopback control port.
#
# Usage:
#   deploy/update.sh                 # full update
#   deploy/update.sh --no-pull       # code is already in place (CI artefact, rsync)
#   deploy/update.sh --no-backup     # skip the dump (you have a fresh one)
#   deploy/update.sh --dry-run       # print the plan, touch nothing
#   DRAIN_TIMEOUT=90 deploy/update.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_DIR="$PWD"

DO_PULL=1
DO_BACKUP=1
DRY_RUN=0
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-60}"
SERVICE="${SERVICE:-autonomous-agent}"
CONTROL_BASE_PORT="${AGENT_CONTROL_BASE_PORT:-28900}"
REGISTRY="$REPO_DIR/configs/accounts/accounts.json"

for arg in "$@"; do
    case "$arg" in
        --no-pull) DO_PULL=0 ;;
        --no-backup) DO_BACKUP=0 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,31p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '    [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------- 0. checks
if [ ! -f "$REGISTRY" ]; then
    echo "no workspace registry at $REGISTRY - is this a deployed checkout?" >&2
    exit 1
fi
if ! command -v curl >/dev/null; then
    echo "curl is required" >&2
    exit 1
fi
if [ "$DO_PULL" = 1 ] && [ -n "$(git status --porcelain)" ]; then
    echo "working tree is not clean - commit, stash or use --no-pull:" >&2
    git status --short >&2
    exit 1
fi

# Every workspace, and whether its agent is answering right now. A control
# port that responds is the only reliable liveness signal: the supervisor's own
# view is in-memory and a restart resets it.
mapfile -t WORKSPACES < <(python3 - "$REGISTRY" "$CONTROL_BASE_PORT" <<'PY'
import json
import sys

registry, base = sys.argv[1], int(sys.argv[2])
data = json.load(open(registry))
rows = data if isinstance(data, list) else data.get("accounts", list(data.values()))
for row in rows:
    print(row["id"], base + int(row.get("index", 0)))
PY
)

# Every control call carries the token the agent wrote at start (see
# control/auth.py). Without it a current agent answers 401, which the health
# probe below would read as "not running" - and skip draining it.
CONTROL_TOKEN_DIR="${AGENT_CONTROL_TOKEN_DIR:-$REPO_DIR/data/runtime/control}"
control_curl() {
    local port="$1"; shift
    local token_file="$CONTROL_TOKEN_DIR/${port}.token"
    if [ -r "$token_file" ]; then
        curl -H "X-Control-Token: $(cat "$token_file")" "$@"
    else
        # An agent from before the token existed neither wrote one nor asks.
        curl "$@"
    fi
}

RUNNING=()
for entry in "${WORKSPACES[@]}"; do
    read -r id port <<<"$entry"
    if control_curl "$port" -fsS -m 2 "http://127.0.0.1:${port}/control/health" >/dev/null 2>&1; then
        RUNNING+=("$id $port")
    fi
done

say "Workspaces: ${#WORKSPACES[@]} configured, ${#RUNNING[@]} running"
for entry in "${RUNNING[@]}"; do
    read -r id port <<<"$entry"
    printf '    running: %-16s control port %s\n' "$id" "$port"
done

# ---------------------------------------------------------------- 1. backup
if [ "$DO_BACKUP" = 1 ]; then
    say "Backing up the database"
    run "$REPO_DIR/deploy/backup.sh"
else
    say "Skipping the backup (--no-backup)"
fi

# ------------------------------------------------------------------ 2. code
if [ "$DO_PULL" = 1 ]; then
    say "Fetching new code"
    run git pull --ff-only
fi
say "Deploying $(git log -1 --format='%h %s')"

# ----------------------------------------------------------------- 3. drain
# Drain before the supervisor restart, not after: the agents are what hold
# customer conversations, and they are the processes that must come back on
# new code.
for entry in "${RUNNING[@]}"; do
    read -r id port <<<"$entry"
    say "Draining $id (up to ${DRAIN_TIMEOUT}s)"
    if [ "$DRY_RUN" = 1 ]; then
        printf '    [dry-run] POST /control/drain then /control/shutdown on %s\n' "$port"
        continue
    fi
    response=$(control_curl "$port" -fsS -m "$((DRAIN_TIMEOUT + 10))" \
        -X POST "http://127.0.0.1:${port}/control/drain" \
        -H 'Content-Type: application/json' \
        -d "{\"timeout_seconds\": ${DRAIN_TIMEOUT}, \"reason\": \"deploy\"}" \
        || echo '{"drained": false, "error": "drain call failed"}')
    printf '    %s\n' "$response"
    if ! printf '%s' "$response" | grep -q '"drained": *true'; then
        echo "" >&2
        echo "    $id did not drain: a task is still running." >&2
        echo "    Stopping it now would cut that task off mid-action." >&2
        echo "    Either wait and run this again, or stop $id from the dashboard" >&2
        echo "    deliberately. Nothing has been restarted yet." >&2
        echo "    To put this workspace back to work without updating:" >&2
        echo "      curl -X POST -H \"X-Control-Token: \$(cat $CONTROL_TOKEN_DIR/${port}.token)\" http://127.0.0.1:${port}/control/resume" >&2
        exit 1
    fi
    say "Stopping $id"
    control_curl "$port" -fsS -m 15 -X POST "http://127.0.0.1:${port}/control/shutdown" >/dev/null \
        || echo "    shutdown call failed; check the workspace by hand" >&2
done

# ------------------------------------------------------------ 4. supervisor
say "Restarting $SERVICE (supervisor + dashboard build)"
if [ "$DRY_RUN" = 1 ]; then
    printf '    [dry-run] sudo systemctl restart %s\n' "$SERVICE"
else
    sudo systemctl restart "$SERVICE"
    # Give it a moment, then say plainly whether it came up.
    sleep 5
    systemctl is-active --quiet "$SERVICE" \
        && echo "    $SERVICE is active" \
        || { echo "    $SERVICE did NOT come up:" >&2
             journalctl -u "$SERVICE" -n 30 --no-pager >&2
             exit 1; }
fi

# ----------------------------------------------------------------- 5. start
say "Done - the supervisor and dashboard now run the new code"
if [ "${#RUNNING[@]}" -eq 0 ]; then
    echo "    No workspace was running, so there is nothing to start again."
    exit 0
fi
cat <<EOF

    Start these workspaces again from the dashboard (Workspace > Start).
    Until then their agents are NOT running and no customer is being answered:

EOF
for entry in "${RUNNING[@]}"; do
    read -r id port <<<"$entry"
    printf '      %s\n' "$id"
done
cat <<'EOF'

    Then check the new code is actually live, per workspace:

      L=data/accounts/<id>/logs/runtime.log
      grep "\[boot\]"               $L | tail -1
      grep "\[MEDIA\] stored"       $L | tail -3
      grep "hold released"          $L | tail -3
      grep "conversation takeover"  $L | tail -3
EOF
