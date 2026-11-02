#!/usr/bin/env bash
# Installs a systemd timer that runs scripts/run_maintenance.py once a day,
# deleting old actions/tasks/notifications rows (see storage/maintenance.py).
#
# Without this, those tables only grow: nothing else in the codebase ever
# calls DatabaseMaintenance outside tests. Left unchecked for months, this is
# what eventually slows every dashboard query and fills the Postgres volume.
#
# A timer rather than cron, matching install_backup_timer.sh - the run lands
# in journald alongside the agent's own logs.
#
# Run once per server, with sudo, from the repo root:
#   sudo ./deploy/install_maintenance_timer.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "!! Needs root. Re-run with sudo from the repo root:" >&2
    echo "     sudo ./deploy/install_maintenance_timer.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
MAINTENANCE_AT="${MAINTENANCE_AT:-04:15}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

if [ ! -f "$REPO_DIR/scripts/run_maintenance.py" ]; then
    echo "!! $REPO_DIR/scripts/run_maintenance.py missing" >&2
    exit 1
fi

echo "==> Writing /etc/systemd/system/autonomous-agent-maintenance.service"
cat > /etc/systemd/system/autonomous-agent-maintenance.service <<EOF
[Unit]
Description=Prune old rows for the autonomous agent database

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=-$REPO_DIR/.env
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/scripts/run_maintenance.py --older-than-days $RETENTION_DAYS --execute
# One run must never hang forever holding the next one off.
TimeoutStartSec=30min
EOF

echo "==> Writing /etc/systemd/system/autonomous-agent-maintenance.timer"
cat > /etc/systemd/system/autonomous-agent-maintenance.timer <<EOF
[Unit]
Description=Daily maintenance for the autonomous agent database

[Timer]
OnCalendar=*-*-* $MAINTENANCE_AT
# A machine that was off at the scheduled time still gets its run once it is
# on again, instead of silently skipping a day.
Persistent=true
# Offset from the backup timer (03:30 by default) so the two never compete
# for the same Postgres connection slot at once.
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now autonomous-agent-maintenance.timer

echo ""
echo "==> Installed and enabled. Next run:"
systemctl list-timers autonomous-agent-maintenance.timer --no-pager | sed -n '1,2p'
echo ""
echo "    Chạy thử ngay (dry-run, không xóa gì): "
echo "      $REPO_DIR/.venv/bin/python $REPO_DIR/scripts/run_maintenance.py"
echo "    Chạy thật ngay:      sudo systemctl start autonomous-agent-maintenance"
echo "    Xem kết quả:         journalctl -u autonomous-agent-maintenance -n 50 --no-pager"
echo "    Đổi số ngày giữ lại: RETENTION_DAYS=60 sudo ./deploy/install_maintenance_timer.sh"
