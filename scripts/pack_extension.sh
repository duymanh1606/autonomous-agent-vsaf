#!/usr/bin/env bash
# Packs extension/ into a signed .crx with a STABLE id (the same RSA key is
# reused across runs, so the extension id never changes across repacks -
# that stability is what lets the Chrome policy below keep pointing at the
# same id forever, including across future edits to the extension's code).
#
# Why this exists at all: modern Chrome (confirmed by direct testing on this
# machine - see docs/ - not assumed) silently refuses to auto-load an
# unpacked extension via `--load-extension` for security reasons, even with
# every workaround flag tried. The only way left to skip the manual
# "chrome://extensions -> Developer mode -> Load unpacked" step is Chrome's
# own enterprise mechanism: a signed .crx + an ExtensionInstallForcelist
# policy (see install_extension_policy.sh, run once per machine after this).
#
# Auto version-bumping: Chrome only treats a repacked .crx as an update if
# its manifest "version" is numerically higher than what's installed - a
# repack with an unchanged version is silently ignored. Editing
# extension/manifest.json's version by hand every time would get forgotten,
# so this script instead packs from a STAGED COPY of extension/ whose
# manifest.json has a 4th version segment appended - an auto-incrementing
# build counter (extension_build/.build_number) - leaving the real,
# human-managed version in extension/manifest.json untouched. A content
# hash (extension_build/.content_hash) skips the repack (and the ~1s
# headless Chrome launch it costs) entirely when extension/ hasn't
# changed, which is what makes it cheap enough to call on every workspace
# Start (see dashboard_api/processes.py) instead of only by hand.
#
# Safe to re-run any time; also safe to call automatically and often.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

EXTENSION_DIR="$(pwd)/extension"
BUILD_DIR="$(pwd)/extension_build"
KEY_PATH="$BUILD_DIR/extension.pem"
CRX_PATH="$BUILD_DIR/extension.crx"
MANIFEST_XML="$BUILD_DIR/update_manifest.xml"
HASH_PATH="$BUILD_DIR/.content_hash"
BUILD_NUMBER_PATH="$BUILD_DIR/.build_number"
STAGING_DIR="$BUILD_DIR/.staging"

mkdir -p "$BUILD_DIR"

if [ ! -f "$KEY_PATH" ]; then
    echo "==> Generating extension signing key (first run only) -> $KEY_PATH"
    openssl genrsa -out "$KEY_PATH" 2048 2>/dev/null
fi

# find | sort makes this independent of directory-walk order; sha256sum of
# that listing is a cheap, good-enough "did anything in extension/ change"
# check (content AND filenames/additions/removals all shift it).
CONTENT_HASH=$(find "$EXTENSION_DIR" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
PREVIOUS_HASH=$(cat "$HASH_PATH" 2>/dev/null || echo "")

if [ "$CONTENT_HASH" = "$PREVIOUS_HASH" ] && [ -f "$CRX_PATH" ] && [ -f "$MANIFEST_XML" ]; then
    echo "==> extension/ unchanged since last pack - skipping (crx: $CRX_PATH)"
    exit 0
fi

BUILD_NUMBER=$(( $(cat "$BUILD_NUMBER_PATH" 2>/dev/null || echo -1) + 1 ))
echo "$BUILD_NUMBER" > "$BUILD_NUMBER_PATH"

BASE_VERSION=$(python3 -c "import json; print(json.load(open('$EXTENSION_DIR/manifest.json'))['version'])")
VERSION="$BASE_VERSION.$BUILD_NUMBER"

rm -rf "$STAGING_DIR"
cp -r "$EXTENSION_DIR" "$STAGING_DIR"
python3 -c "
import json
path = '$STAGING_DIR/manifest.json'
manifest = json.load(open(path))
manifest['version'] = '$VERSION'
json.dump(manifest, open(path, 'w'), indent=2)
"

echo "==> Packing $EXTENSION_DIR -> $CRX_PATH (version $VERSION)"
# --pack-extension writes the .crx next to the source dir (extension_build/
# .staging.crx here, not to an arbitrary --out path) - move it into place.
# --no-sandbox: Chrome refuses to start at all as root otherwise ("Running
# as root without --no-sandbox is not supported") - confirmed live on a
# server where this script is run as root. Harmless here: this invocation
# only packs a local directory into a .crx, it never renders any web
# content, so there's nothing the sandbox would have been protecting.
rm -f "$BUILD_DIR/.staging.crx"
PACK_LOG="$BUILD_DIR/.pack.log"
if ! google-chrome --headless=new --no-sandbox \
    --pack-extension="$STAGING_DIR" \
    --pack-extension-key="$KEY_PATH" >"$PACK_LOG" 2>&1; then
    echo "!! google-chrome --pack-extension failed:" >&2
    cat "$PACK_LOG" >&2
    rm -rf "$STAGING_DIR"
    exit 1
fi
mv "$BUILD_DIR/.staging.crx" "$CRX_PATH"
rm -rf "$STAGING_DIR"

EXTENSION_ID=$(
    openssl rsa -in "$KEY_PATH" -pubout -outform DER 2>/dev/null \
        | python3 -c "
import hashlib, sys
digest = hashlib.sha256(sys.stdin.buffer.read()).digest()[:16]
print(''.join(chr(ord('a') + (b >> 4)) + chr(ord('a') + (b & 0xf)) for b in digest))
"
)

cat > "$MANIFEST_XML" <<EOF
<?xml version='1.0' encoding='UTF-8'?>
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='$EXTENSION_ID'>
    <updatecheck codebase='file://$CRX_PATH' version='$VERSION' />
  </app>
</gupdate>
EOF

echo "$EXTENSION_ID" > "$BUILD_DIR/extension_id.txt"
echo "$CONTENT_HASH" > "$HASH_PATH"
echo "$VERSION" > "$BUILD_DIR/.version"

echo "==> Done."
echo "    Extension id:    $EXTENSION_ID"
echo "    Version:         $VERSION"
echo "    CRX:             $CRX_PATH"
echo "    Update manifest: $MANIFEST_XML"
echo ""
echo "Next (only the FIRST time, or if this id/path ever changes):"
echo "    sudo ./scripts/install_extension_policy.sh"
