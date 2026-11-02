#!/usr/bin/env bash
# One backup run: the PostgreSQL database plus every account's product
# catalogue, into one timestamped, compressed archive.
#
# What is deliberately NOT backed up: the Chrome profiles under
# ~/.autonomous-agent-chrome (2.5GB, and the largest thing on disk). They
# hold TikTok session cookies, which are tied to the device that created
# them - restoring them onto a *different* machine, which is precisely the
# disaster this backup exists for, is the case least likely to work. Backing
# them up would turn every archive into a credential store for that small
# chance. The recovery path for a lost profile is a manual TikTok login per
# workspace instead; see deploy/README.md.
#
# Safe to run against a live system: pg_dump takes a consistent MVCC snapshot
# without locking writers, and the catalogue files are copied, never moved.
#
# Encryption: with BACKUP_AGE_RECIPIENT set, the archive is encrypted with age
# to that PUBLIC key before it touches the disk, and only the encrypted file
# exists - locally and offsite. The matching private key is kept OFF this
# server (password manager, the operator's laptop): an archive holds every
# customer conversation and every workspace's API key, and a copy in cloud
# storage must be useless to whoever reads it there. Without a recipient the
# archive stays plaintext and local - pushing it offsite is refused unless
# BACKUP_ALLOW_PLAINTEXT_REMOTE=1 says so explicitly.
#
# Every run, good or bad, writes $BACKUP_DIR/last-run.json; the supervisor's
# health monitor reads it, so a failed offsite copy reaches the operator
# (dashboard + ALERT_EMAILS) instead of only journald.
#
# Usage (from anywhere):
#   ./deploy/backup.sh
# Overridable (environment, or .env):
#   BACKUP_DIR                     where archives land       (default: <repo>/data/backups)
#   BACKUP_KEEP                    how many to keep locally  (default: 7)
#   BACKUP_REMOTE                  rclone/rsync destination  (default: none - local only)
#   BACKUP_REMOTE_KEEP_DAYS        rclone only: delete remote archives older than this (default: 30, 0 = never)
#   BACKUP_AGE_RECIPIENT           age public key(s) "age1..." (comma/space separated) or a recipients file
#   BACKUP_ALLOW_PLAINTEXT_REMOTE  1 = allow an unencrypted archive offsite (not recommended)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/data/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"

# .env carries POSTGRES_USER/POSTGRES_DB, which the dump needs to name the
# right database. Read as plain KEY=value text, never sourced: sourcing runs
# the file as shell, so a value with `$` or ` #` in it (an API key, a
# password) is silently mangled - `ab$cd#ef` comes back as `ab#ef`. The
# project's own loader (load_config.load_env_file) treats .env the same way.
env_value() {
    # First match wins; strips one layer of matching quotes; a variable already
    # in the environment outranks the file, as everywhere else in the project.
    local name="$1" line value
    if [ -n "${!name:-}" ]; then printf '%s' "${!name}"; return; fi
    [ -f "$REPO_DIR/.env" ] || return 0
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$REPO_DIR/.env" | head -n1)" || return 0
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"   # ltrim
    value="${value%"${value##*[![:space:]]}"}"   # rtrim
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}
POSTGRES_USER="$(env_value POSTGRES_USER)"; POSTGRES_USER="${POSTGRES_USER:-agent}"
POSTGRES_DB="$(env_value POSTGRES_DB)";     POSTGRES_DB="${POSTGRES_DB:-agent}"
BACKUP_REMOTE="${BACKUP_REMOTE:-$(env_value BACKUP_REMOTE)}"
BACKUP_REMOTE_KEEP_DAYS="${BACKUP_REMOTE_KEEP_DAYS:-$(env_value BACKUP_REMOTE_KEEP_DAYS)}"
BACKUP_REMOTE_KEEP_DAYS="${BACKUP_REMOTE_KEEP_DAYS:-30}"
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-$(env_value BACKUP_AGE_RECIPIENT)}"
BACKUP_ALLOW_PLAINTEXT_REMOTE="${BACKUP_ALLOW_PLAINTEXT_REMOTE:-$(env_value BACKUP_ALLOW_PLAINTEXT_REMOTE)}"

STAMP="$(date +%Y%m%d-%H%M%S)"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
WORK="$(mktemp -d)"
mkdir -p "$BACKUP_DIR"
STATUS_FILE="$BACKUP_DIR/last-run.json"

# What the status file reports; each step below updates its own field.
ARCHIVE=""
ENCRYPTED=false
REMOTE_STATE="disabled"   # disabled | ok | failed | refused_plaintext
ERROR=""

json_string() {
    local value="${1//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '"%s"' "$value"
}

write_status() {
    local ok="$1"
    {
        printf '{"started_at": %s, "finished_at": %s, "ok": %s, ' \
            "$(json_string "$STARTED_AT")" "$(json_string "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" "$ok"
        printf '"archive": %s, "encrypted": %s, "remote": %s, "error": %s}\n' \
            "$(json_string "$(basename "${ARCHIVE:-}")")" "$ENCRYPTED" \
            "$(json_string "$REMOTE_STATE")" "$(json_string "$ERROR")"
    } > "$STATUS_FILE.tmp"
    mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

fail() {
    ERROR="$1"
    echo "!! $1" >&2
    exit 1
}

# A failed run must not leave half an archive behind to be mistaken for a
# good one, nor leak a temp directory holding a copy of the database - and it
# must say it failed, which is what the status file is for.
on_exit() {
    local code=$?
    rm -rf "$WORK"
    rm -f "$BACKUP_DIR"/backup-*.partial
    if [ "$code" -ne 0 ] && [ -z "$ERROR" ]; then
        ERROR="backup.sh exited with status $code"
    fi
    if [ "$code" -eq 0 ]; then write_status true; else write_status false; fi
}
trap on_exit EXIT

echo "==> Backup $STAMP"

# -- database ---------------------------------------------------------------
# Run pg_dump *inside* the container: the host has no PostgreSQL client
# installed, and requiring one would add a second thing to keep in step with
# the server's major version. --no-privileges: the dump must restore onto a
# fresh server where the agent_tenant role does not exist yet; the role and its
# grants are recreated at the next start (storage/tenancy.py). The RLS
# policies themselves are schema, and are in the dump.
echo "    pg_dump $POSTGRES_DB"
if ! docker compose exec -T postgres \
        pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-privileges \
        > "$WORK/database.sql"; then
    fail "pg_dump failed - is the postgres container up? (docker compose ps)"
fi

# An empty or truncated dump is worse than no dump, because it looks like a
# backup. Refuse to archive one.
if [ ! -s "$WORK/database.sql" ]; then
    fail "pg_dump produced an empty file - refusing to write a useless archive"
fi

# -- product catalogues -----------------------------------------------------
# Per-account product.json files and their uploaded media, which live on disk
# rather than in PostgreSQL (storage/product_repository.py).
if [ -d "$REPO_DIR/data/accounts" ]; then
    echo "    data/accounts/"
    mkdir -p "$WORK/accounts"
    cp -a "$REPO_DIR/data/accounts/." "$WORK/accounts/"
fi

# -- workspace configs ------------------------------------------------------
# configs/accounts/*.yaml plus accounts.json: which workspaces exist, their
# ports and profile directory names. Small, and without them a restored
# database describes accounts the supervisor does not know how to start.
if [ -d "$REPO_DIR/configs/accounts" ]; then
    echo "    configs/accounts/"
    mkdir -p "$WORK/configs"
    cp -a "$REPO_DIR/configs/accounts/." "$WORK/configs/"
fi

# -- shared config and secrets -------------------------------------------------
# Workspace YAMLs are overlays on configs/config.yaml (load_config.EXTENDS_KEY),
# so they mean nothing without the base they were written against - which may
# carry server-side edits that are in no git commit.
if [ -f "$REPO_DIR/configs/config.yaml" ]; then
    echo "    configs/config.yaml"
    cp -a "$REPO_DIR/configs/config.yaml" "$WORK/config.yaml"
fi
# .env holds the database password, SMTP and API keys: rebuilding a server
# without it means re-collecting every one of them. Only ever inside an
# encrypted archive.
if [ -n "$BACKUP_AGE_RECIPIENT" ] && [ -f "$REPO_DIR/.env" ]; then
    echo "    .env (encrypted archive only)"
    cp -a "$REPO_DIR/.env" "$WORK/env"
fi

# -- archive ----------------------------------------------------------------
# Built inside $WORK and checked before anything is written to $BACKUP_DIR.
mkdir -p "$WORK/out"
tar -czf "$WORK/out/archive.tar.gz" -C "$WORK" --exclude=./out .
gzip -t "$WORK/out/archive.tar.gz" || fail "the archive failed its own integrity check"

if [ -n "$BACKUP_AGE_RECIPIENT" ]; then
    command -v age >/dev/null 2>&1 || fail "BACKUP_AGE_RECIPIENT is set but age is not installed (sudo apt install age)"
    AGE_ARGS=()
    if [ -f "$BACKUP_AGE_RECIPIENT" ]; then
        AGE_ARGS+=(-R "$BACKUP_AGE_RECIPIENT")
    else
        for key in ${BACKUP_AGE_RECIPIENT//,/ }; do AGE_ARGS+=(-r "$key"); done
    fi
    ARCHIVE="$BACKUP_DIR/backup-$STAMP.tar.gz.age"
    age "${AGE_ARGS[@]}" -o "$ARCHIVE.partial" "$WORK/out/archive.tar.gz" \
        || fail "age encryption failed - check BACKUP_AGE_RECIPIENT"
    [ -s "$ARCHIVE.partial" ] || fail "age produced an empty file"
    ENCRYPTED=true
else
    ARCHIVE="$BACKUP_DIR/backup-$STAMP.tar.gz"
    cp "$WORK/out/archive.tar.gz" "$ARCHIVE.partial"
fi
# Written under a .partial name and renamed only on success, so an
# interrupted run never leaves something rotation would count as good.
mv "$ARCHIVE.partial" "$ARCHIVE"
chmod 600 "$ARCHIVE"
echo "    -> $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1), encrypted=$ENCRYPTED)"

# -- offsite ----------------------------------------------------------------
# A backup on the same disk as the thing it backs up only survives mistakes,
# not hardware. This step is what makes it survive losing the machine. Its
# failure does not undo the local archive (rotation still runs) but the run
# exits non-zero and the status file says why.
OFFSITE_FAILED=""
if [ -z "$BACKUP_REMOTE" ]; then
    echo "    (BACKUP_REMOTE not set - local only; this does not survive losing the disk)"
elif [ "$ENCRYPTED" != true ] && [ "$BACKUP_ALLOW_PLAINTEXT_REMOTE" != 1 ]; then
    REMOTE_STATE="refused_plaintext"
    OFFSITE_FAILED="refusing to copy an unencrypted archive offsite: set BACKUP_AGE_RECIPIENT (deploy/README.md, section 7)"
else
    echo "    copying to $BACKUP_REMOTE"
    if command -v rclone >/dev/null 2>&1; then
        if rclone copy "$ARCHIVE" "$BACKUP_REMOTE"; then
            REMOTE_STATE="ok"
            # rclone copy never deletes, so the remote would grow forever.
            if [ "$BACKUP_REMOTE_KEEP_DAYS" -gt 0 ] 2>/dev/null; then
                rclone delete "$BACKUP_REMOTE" --min-age "${BACKUP_REMOTE_KEEP_DAYS}d" \
                    --include "backup-*" \
                    || echo "    (warning: could not prune remote archives older than ${BACKUP_REMOTE_KEEP_DAYS}d)" >&2
            fi
        else
            REMOTE_STATE="failed"
        fi
    elif rsync -a "$ARCHIVE" "$BACKUP_REMOTE"; then
        REMOTE_STATE="ok"
    else
        REMOTE_STATE="failed"
    fi
    [ "$REMOTE_STATE" = ok ] || OFFSITE_FAILED="copy to BACKUP_REMOTE failed"
fi

# -- rotation ---------------------------------------------------------------
# Rotate only after a successful archive, so a run of failures can never
# delete the last good backup.
# Plain and encrypted archives count together: switching encryption on must
# not keep the old plaintext ones around forever.
list_archives() {
    ls -1t "$BACKUP_DIR"/backup-*.tar.gz "$BACKUP_DIR"/backup-*.tar.gz.age 2>/dev/null || true
}
mapfile -t OLD < <(list_archives | tail -n +"$((BACKUP_KEEP + 1))")
if [ "${#OLD[@]}" -gt 0 ]; then
    echo "    rotating out ${#OLD[@]} old archive(s)"
    rm -f "${OLD[@]}"
fi

if [ -n "$OFFSITE_FAILED" ]; then
    fail "$OFFSITE_FAILED"
fi
echo "==> Done. $(list_archives | wc -l) archive(s) kept."
