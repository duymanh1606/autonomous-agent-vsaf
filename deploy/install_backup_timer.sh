#!/usr/bin/env bash
# Installs a systemd timer that runs deploy/backup.sh once a day.
#
# A timer rather than cron, to match how the rest of this deployment is run
# (see install_systemd_service.sh): the run lands in journald alongside the
# agent's own logs, and `systemctl status` answers "did last night's backup
# work?" without hunting for a log file.
#
# Run once per server, with sudo, from the repo root:
#   sudo ./deploy/install_backup_timer.sh
#
# To also copy each archive off this machine - which is what makes the backup
# survive losing the disk - set BACKUP_REMOTE in .env to an rclone remote
# (e.g. BACKUP_REMOTE=gdrive:agent-backups) or an rsync target, then re-run.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "!! Needs root. Re-run with sudo from the repo root:" >&2
    echo "     sudo ./deploy/install_backup_timer.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
BACKUP_AT="${BACKUP_AT:-03:30}"

if [ ! -x "$REPO_DIR/deploy/backup.sh" ]; then
    echo "!! $REPO_DIR/deploy/backup.sh missing or not executable" >&2
    exit 1
fi

# backup.sh talks to the postgres container, so the user running it needs to
# reach the Docker socket. Checking here turns a silent 3:30am failure into a
# message now.
if ! su - "$RUN_USER" -c "docker info" >/dev/null 2>&1; then
    echo "!! $RUN_USER cannot talk to Docker - the timer would fail every night." >&2
    echo "   Fix with: sudo usermod -aG docker $RUN_USER  (then log out and back in)" >&2
    exit 1
fi

# Same reasoning for encryption: a recipient without the tool fails every run.
if grep -qE '^[[:space:]]*(export[[:space:]]+)?BACKUP_AGE_RECIPIENT=.+' "$REPO_DIR/.env" 2>/dev/null \
        && ! command -v age >/dev/null 2>&1; then
    echo "!! BACKUP_AGE_RECIPIENT is set in .env but age is not installed." >&2
    echo "   Fix with: sudo apt install age" >&2
    exit 1
fi
if grep -qE '^[[:space:]]*(export[[:space:]]+)?BACKUP_REMOTE=.+' "$REPO_DIR/.env" 2>/dev/null \
        && ! grep -qE '^[[:space:]]*(export[[:space:]]+)?BACKUP_AGE_RECIPIENT=.+' "$REPO_DIR/.env"; then
    echo "!! BACKUP_REMOTE is set but BACKUP_AGE_RECIPIENT is not: backup.sh will refuse" >&2
    echo "   to copy an unencrypted archive offsite. See deploy/README.md section 7." >&2
fi

echo "==> Writing /etc/systemd/system/autonomous-agent-backup.service"
cat > /etc/systemd/system/autonomous-agent-backup.service <<EOF
[Unit]
Description=Backup the autonomous agent database and product catalogues
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/deploy/backup.sh
# One run must never hang forever holding the next one off.
TimeoutStartSec=30min
EOF

echo "==> Writing /etc/systemd/system/autonomous-agent-backup.timer"
cat > /etc/systemd/system/autonomous-agent-backup.timer <<EOF
[Unit]
Description=Daily backup for the autonomous agent

[Timer]
OnCalendar=*-*-* $BACKUP_AT
# A machine that was off at 03:30 still gets its backup once it is on again,
# instead of silently skipping a day.
Persistent=true
# Keeps several machines (or a restart storm) from all dumping at once.
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now autonomous-agent-backup.timer

echo ""
echo "==> Installed and enabled. Next run:"
systemctl list-timers autonomous-agent-backup.timer --no-pager | sed -n '1,2p'
echo ""
echo "    Chạy thử ngay:      sudo systemctl start autonomous-agent-backup"
echo "    Xem kết quả:        journalctl -u autonomous-agent-backup -n 30 --no-pager"
echo "    Bản sao lưu nằm ở:  $REPO_DIR/data/backups"
if ! grep -q '^BACKUP_REMOTE=' "$REPO_DIR/.env" 2>/dev/null; then
    echo ""
    echo "    ⚠ BACKUP_REMOTE chưa đặt trong .env - bản sao lưu vẫn nằm trên cùng ổ đĩa"
    echo "      với dữ liệu gốc, nên KHÔNG sống sót khi hỏng ổ. Đặt nó rồi chạy lại script này."
fi
