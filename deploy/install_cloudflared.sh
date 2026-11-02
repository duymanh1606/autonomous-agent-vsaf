#!/usr/bin/env bash
# Installs and configures cloudflared so the dashboard is reachable from the
# internet without opening a single inbound port - the tunnel dials out.
#
# Two phases, because one step in the middle cannot be automated: creating a
# tunnel needs `cloudflared tunnel login`, which opens a browser and asks you
# to pick a zone. So:
#
#   sudo ./deploy/install_cloudflared.sh install
#       Installs the package from Cloudflare's apt repo and stops. Prints the
#       three interactive commands to run next, as yourself, not as root.
#
#   sudo ./deploy/install_cloudflared.sh configure app.example.com
#       After those three: writes /etc/cloudflared/config.yml, locks down the
#       credentials file, installs the service and starts it.
#
# What this deliberately does NOT do: touch .env, restart the agent, or change
# DASHBOARD_LISTEN_ADDR. Binding to loopback before the tunnel is proven
# working locks everyone out (trap A in docs/cloudflare-tunnel.md), so that
# step stays a deliberate, separate decision you make after verifying.
#
# Needs a domain already on Cloudflare (nameservers pointed at it). Without
# one there is nothing to route a hostname to - see docs/cloudflare-tunnel.md.
set -euo pipefail

CONFIG_DIR=/etc/cloudflared
TUNNEL_NAME="${TUNNEL_NAME:-autonomous-agent}"
DASHBOARD_PORT="${DASHBOARD_PORT:-25173}"

die() { echo "!! $*" >&2; exit 1; }

usage() {
    cat >&2 <<USAGE
Usage:
  sudo ./deploy/install_cloudflared.sh install
  sudo ./deploy/install_cloudflared.sh configure app.example.com
USAGE
    exit "${1:-1}"
}

# Argument checking before the root check, on purpose: running this with no
# arguments should explain itself, not demand sudo and only then explain.
case "${1:-}" in
    install)   ;;
    configure) [ -n "${2:-}" ] || { echo "!! configure needs a hostname" >&2; usage; } ;;
    -h|--help) usage 0 ;;
    *) usage ;;
esac

[ "$(id -u)" -eq 0 ] || die "Needs root. Re-run with sudo:
     sudo ./deploy/install_cloudflared.sh $*"

RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

do_install() {
    command -v apt-get >/dev/null || die "Not a Debian/Ubuntu box - install cloudflared by hand."

    if command -v cloudflared >/dev/null; then
        echo "==> cloudflared already installed: $(cloudflared --version)"
    else
        echo "==> Adding Cloudflare's apt repository"
        install -d -m 0755 /usr/share/keyrings
        curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
            > /usr/share/keyrings/cloudflare-main.gpg
        echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
            > /etc/apt/sources.list.d/cloudflared.list
        apt-get update -qq
        apt-get install -y cloudflared
    fi

    cat <<NEXT

==> Installed. Now run these three AS YOURSELF (not with sudo) - the first
    opens a browser and needs you to pick the zone:

      cloudflared tunnel login
      cloudflared tunnel create $TUNNEL_NAME
      cloudflared tunnel route dns $TUNNEL_NAME <hostname>

    They write ~/.cloudflared/. Then come back and run:

      sudo ./deploy/install_cloudflared.sh configure <hostname>

NEXT
}

do_configure() {
    local hostname="$1"
    case "$hostname" in
        *.*) ;;
        *) die "'$hostname' is not a hostname - expected something like app.example.com" ;;
    esac
    command -v cloudflared >/dev/null || die "cloudflared not installed - run the 'install' phase first."

    # `tunnel create` writes <tunnel-id>.json into the *calling user's* home,
    # which is why this has to look there rather than in root's.
    local home_dir cred
    home_dir="$(getent passwd "$RUN_USER" | cut -d: -f6)"
    cred="$(find "$home_dir/.cloudflared" -maxdepth 1 -name '*.json' 2>/dev/null | head -1)"
    [ -n "$cred" ] || die "No tunnel credentials in $home_dir/.cloudflared.
   Run 'cloudflared tunnel create $TUNNEL_NAME' as $RUN_USER first."

    install -d -m 0755 "$CONFIG_DIR"
    install -m 0600 "$cred" "$CONFIG_DIR/"
    local cred_name
    cred_name="$(basename "$cred")"
    echo "==> Credentials: $CONFIG_DIR/$cred_name (0600 - this file is a key)"

    # The catch-all 404 is not optional and must be last: without it every
    # hostname that resolves to this tunnel gets forwarded into the machine,
    # including ones you never meant to serve.
    cat > "$CONFIG_DIR/config.yml" <<YAML
tunnel: $TUNNEL_NAME
credentials-file: $CONFIG_DIR/$cred_name

ingress:
  - hostname: $hostname
    service: http://127.0.0.1:$DASHBOARD_PORT
  # Required, and required to be last. Anything not matched above is refused
  # here rather than reaching the host.
  - service: http_status:404
YAML
    chmod 0644 "$CONFIG_DIR/config.yml"
    echo "==> Wrote $CONFIG_DIR/config.yml -> http://127.0.0.1:$DASHBOARD_PORT"

    cloudflared --config "$CONFIG_DIR/config.yml" tunnel ingress validate \
        || die "cloudflared rejected the config above."

    # Deliberately no Requires= on autonomous-agent: the two are independent
    # in both directions. The agent dying must not take the tunnel with it -
    # that tunnel is how you get in to look at the logs.
    cloudflared service install 2>/dev/null || true
    systemctl enable --now cloudflared
    systemctl --no-pager --lines=5 status cloudflared || true

    cat <<NEXT

==> Tunnel up. Still to do, in this order - none of it is done for you:

  1. Zero Trust -> Access -> Applications -> Add -> Self-hosted
       Application domain: $hostname
       Policy: Allow, by specific emails or an Access Group
     Copy the Application Audience (AUD) tag.

  2. In .env:
       DASHBOARD_ENV=production
       DASHBOARD_PUBLIC_URL=https://$hostname
       DASHBOARD_TRUSTED_ORIGINS=https://$hostname,http://127.0.0.1:25174
       CF_ACCESS_TEAM_DOMAIN=<yourteam>.cloudflareaccess.com
       CF_ACCESS_AUD=<the AUD tag>
     Then: sudo systemctl restart autonomous-agent

  3. VERIFY BEFORE LOCKING THE DOOR. Open https://$hostname in a private
     window: you must hit Cloudflare Access first, then the dashboard login.
     An incognito window matters - an existing Access session hides exactly
     the misconfiguration you are testing for.

  4. Only once step 3 is green, set DASHBOARD_LISTEN_ADDR=127.0.0.1 in .env
     and run: docker compose up -d --build dashboard
     Doing this earlier leaves ssh -L as the only way in.

  Full context, including what must never be routed through the tunnel:
  docs/cloudflare-tunnel.md

NEXT
}

case "$1" in
    install)   do_install ;;
    configure) do_configure "$2" ;;
esac
