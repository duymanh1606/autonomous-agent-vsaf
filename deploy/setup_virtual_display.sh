#!/usr/bin/env bash
# Installs the packages each workspace's own display stack needs to render
# Chrome and let an operator see/click the real pixels (login, CAPTCHA,
# message requests) - not a screenshot, a genuine X display.
#
# What this script used to also do, and no longer does: write 4 static
# systemd units (xvfb.service, fluxbox.service, x11vnc.service, novnc.service)
# sharing ONE display for every workspace on the machine. That meant anyone
# who opened noVNC saw and could click into every workspace's Chrome, not
# just the one they were authorized for. Each workspace now gets its OWN
# Xvfb + x11vnc + websockify, spawned and torn down by the supervisor itself
# alongside that workspace's Chrome + agent (dashboard_api/processes.py's
# AccountProcesses.start_chrome/stop_all) - the same model Chrome and the
# agent have always used, and neither of THEM has ever had a systemd unit of
# its own either. A resource created and destroyed per-workspace at runtime
# (via the dashboard's own Start/Stop) does not fit a static, install-time
# systemd unit; installing the packages is still a one-time, per-machine step
# (the same binaries are reused by every workspace's own instance), so that
# part of this script stays.
#
# fluxbox and xdotool are dropped from the install list: fluxbox existed only
# to keep several workspaces' windows on one shared screen from opening on
# top of each other, and xdotool only to relabel/raise windows on that same
# shared screen - neither problem exists once each workspace has exactly one
# window on its own dedicated display.
#
# Run once per server, with sudo:
#   sudo ./deploy/setup_virtual_display.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "!! Needs root. Re-run with sudo:" >&2
    echo "     sudo ./deploy/setup_virtual_display.sh" >&2
    exit 1
fi

echo "==> Installing Xvfb + x11vnc + noVNC + xauth"
apt-get update -qq
# ffmpeg provides ffprobe: the agent measures every video before sending it and
# refuses one it cannot measure, so without it no video ever reaches a customer.
apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify xauth ffmpeg >/dev/null

echo ""
echo "==> Done. Nothing is running yet - each workspace's own Xvfb/x11vnc/"
echo "    noVNC starts the first time it is Started from the dashboard, and"
echo "    stops when it is Stopped (dashboard_api/processes.py). See"
echo "    docs and .env.example for AGENT_DISPLAY_BASE/AGENT_VNC_BASE_PORT/"
echo "    AGENT_NOVNC_BASE_PORT if the defaults (display :100+i, VNC"
echo "    25901+i, noVNC 26081+i) need to move on this machine."
echo ""
echo "==> Upgrading a server that already has the OLD shared setup running?"
echo "    Stop every workspace from the dashboard first (its Chrome is about"
echo "    to lose the display it's pointed at), then:"
echo "      sudo systemctl stop novnc x11vnc fluxbox xvfb"
echo "      sudo systemctl disable novnc x11vnc fluxbox xvfb"
echo "      sudo rm -f /etc/systemd/system/{xvfb,fluxbox,x11vnc,novnc}.service"
echo "      sudo systemctl daemon-reload"
echo "    The new per-workspace ports (100+/25901+/26081+) never overlap the"
echo "    old fixed ones (99/25900/26080) even if this step is skipped, but"
echo "    doing it keeps an unused fluxbox/xdotool-less setup off the machine."
