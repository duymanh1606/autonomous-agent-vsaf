#!/usr/bin/env bash
# Installs the Chrome enterprise policy that force-installs the DOM bridge
# extension into EVERY Chrome profile on this machine - including profiles
# for workspaces created after this script runs. Needs root because Chrome
# deliberately only reads policy from an admin-writable path (a regular
# user, or malware running as one, must not be able to grant itself this
# power - the same reasoning behind Chrome now refusing --load-extension).
#
# Run once per machine, after ./scripts/pack_extension.sh has produced
# extension_build/extension_id.txt and extension_build/update_manifest.xml.
# Safe to re-run (overwrites the same policy file).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ "$(id -u)" -ne 0 ]; then
    echo "!! Needs root (writes /etc/opt/chrome/policies/managed/). Re-run with sudo:" >&2
    echo "     sudo ./scripts/install_extension_policy.sh" >&2
    exit 1
fi

BUILD_DIR="$(pwd)/extension_build"
ID_FILE="$BUILD_DIR/extension_id.txt"
MANIFEST_XML="$BUILD_DIR/update_manifest.xml"

if [ ! -f "$ID_FILE" ] || [ ! -f "$MANIFEST_XML" ]; then
    echo "!! Run ./scripts/pack_extension.sh first (as your normal user, not root)." >&2
    exit 1
fi

EXTENSION_ID=$(cat "$ID_FILE")
POLICY_DIR="/etc/opt/chrome/policies/managed"
POLICY_FILE="$POLICY_DIR/autonomous-agent-extension.json"

mkdir -p "$POLICY_DIR"
cat > "$POLICY_FILE" <<EOF
{
  "ExtensionInstallForcelist": [
    "$EXTENSION_ID;file://$MANIFEST_XML"
  ]
}
EOF
chmod 644 "$POLICY_FILE"

echo "==> Wrote $POLICY_FILE"
echo "    Extension id: $EXTENSION_ID"
echo ""
echo "Takes effect the next time Chrome starts for any profile on this"
echo "machine - no per-workspace step needed from here on. If a Chrome"
echo "profile for an existing workspace is already open, restart it once."
