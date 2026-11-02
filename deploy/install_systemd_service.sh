#!/usr/bin/env bash
# Installs a systemd service that runs ./deploy.sh (Postgres + dashboard
# container + supervisor) so it survives an SSH disconnect and auto-restarts
# on crash - the two problems a plain SSH session has (the process dies on
# SIGHUP when the terminal closes, and nothing brings it back after a crash
# or reboot).
#
# Run after ./deploy/setup_virtual_display.sh (installs the Xvfb/x11vnc/noVNC
# packages this needs - each workspace now spawns its own instance of them at
# Start time, not a systemd unit this service depends on).
#
# Run once per server, with sudo, from the repo root:
#   sudo ./deploy/install_systemd_service.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "!! Needs root. Re-run with sudo from the repo root:" >&2
    echo "     sudo ./deploy/install_systemd_service.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

if [ ! -f "$REPO_DIR/.env" ]; then
    echo "!! $REPO_DIR/.env not found - create it first (cp .env.example .env, fill it in)." >&2
    exit 1
fi

# systemd services run with a minimal default PATH, not the interactive
# shell PATH `uv` gets installed into (typically ~/.local/bin) - without
# this, deploy.sh's `uv run ...` fails with "uv: command not found" and the
# service exits immediately (confirmed live: this is exactly what happened
# on first install here, not a hypothetical). Resolve uv's real directory as
# the target user, not root, since `sudo` alone would look in root's PATH.
UV_DIR="$(su - "$RUN_USER" -c 'command -v uv' 2>/dev/null | xargs -r dirname || true)"
SERVICE_PATH="${UV_DIR:+$UV_DIR:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if [ -z "$UV_DIR" ]; then
    echo "!! Could not find 'uv' on $RUN_USER's PATH - deploy.sh will fail the same way" >&2
    echo "   it just did (\"uv: command not found\") until this is fixed. Install uv for" >&2
    echo "   $RUN_USER first, then re-run this script." >&2
fi

echo "==> Writing /etc/systemd/system/autonomous-agent.service"
echo "    repo=$REPO_DIR user=$RUN_USER uv_dir=${UV_DIR:-NOT FOUND}"

cat > /etc/systemd/system/autonomous-agent.service <<EOF
[Unit]
Description=Autonomous TikTok agent (Postgres + dashboard + supervisor)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
Environment=PATH=$SERVICE_PATH
EnvironmentFile=$REPO_DIR/.env
ExecStart=$REPO_DIR/deploy.sh
# Stop/restart signals only the supervisor. The default (control-group) kills
# every process in the unit's cgroup - and each workspace's agent, Chrome and
# Xvfb/VNC live there too, since start_new_session leaves the session, not the
# cgroup. So every restart, or any supervisor crash, cut every conversation in
# flight, and the adopt-on-boot path never had anything left to adopt.
# Needs deploy.sh to exec the supervisor, or the signal stops at bash.
KillMode=process
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable autonomous-agent.service

# A bad .env is the one mistake that leaves this unit looping on restart with
# nothing but `command not found` in the journal - say so now, not at start.
"$REPO_DIR/deploy/check_env.sh" "$REPO_DIR/.env" || \
    echo "!! Sửa .env theo thông báo trên trước khi start, nếu không service sẽ không lên." >&2

echo ""
echo "==> Installed but not started yet. Start it with:"
echo "      sudo systemctl start autonomous-agent"
echo "    Xem log:"
echo "      journalctl -u autonomous-agent -f"
echo "    Tự khởi động lại khi crash hoặc reboot server - không cần giữ phiên SSH mở."
