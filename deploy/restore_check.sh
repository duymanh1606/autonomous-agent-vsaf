#!/usr/bin/env bash
# Restore drill: prove an archive from deploy/backup.sh can actually be restored.
#
# A backup nobody has restored is a hope, not a backup. This restores one
# archive into a THROWAWAY database next to the live one, checks what came
# back against the live database, reports how long it took (the restore part
# of the RTO), and drops the throwaway database again. The live database,
# configs and catalogue are never written.
#
# Usage, from the repo root:
#   deploy/restore_check.sh data/backups/backup-<stamp>.tar.gz
#   deploy/restore_check.sh backup-<stamp>.tar.gz.age -i ~/backup-key.txt
#
# For the full drill, fetch the archive from BACKUP_REMOTE first (that is
# the copy a lost server leaves you), and use the private key from wherever it
# is really kept - the drill is also the test that both can still be found:
#   rclone copy "$BACKUP_REMOTE/backup-<stamp>.tar.gz.age" /tmp/drill/
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

ARCHIVE=""
IDENTITY=""
while [ $# -gt 0 ]; do
    case "$1" in
        -i|--identity) IDENTITY="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) ARCHIVE="$1"; shift ;;
    esac
done
[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || { echo "!! archive not found: ${ARCHIVE:-<none given>}" >&2; exit 2; }

# Same .env reading as deploy/backup.sh: plain KEY=value text, never sourced.
env_value() {
    local name="$1" line value
    if [ -n "${!name:-}" ]; then printf '%s' "${!name}"; return; fi
    [ -f "$REPO_DIR/.env" ] || return 0
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$REPO_DIR/.env" | head -n1)" || return 0
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}
POSTGRES_USER="$(env_value POSTGRES_USER)"; POSTGRES_USER="${POSTGRES_USER:-agent}"
POSTGRES_DB="$(env_value POSTGRES_DB)";     POSTGRES_DB="${POSTGRES_DB:-agent}"

DRILL_DB="restore_check_$(date +%Y%m%d%H%M%S)"
WORK="$(mktemp -d)"
chmod 700 "$WORK"
psql_in() {  # psql_in <database> [psql args...]
    local db="$1"; shift
    docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$db" -v ON_ERROR_STOP=1 "$@"
}
cleanup() {
    rm -rf "$WORK"
    psql_in postgres -q -c "DROP DATABASE IF EXISTS $DRILL_DB;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

START=$(date +%s)
echo "==> Restore drill: $(basename "$ARCHIVE")"

# -- decrypt + unpack -------------------------------------------------------
TARBALL="$ARCHIVE"
if [[ "$ARCHIVE" == *.age ]]; then
    command -v age >/dev/null 2>&1 || { echo "!! age is not installed (sudo apt install age)" >&2; exit 1; }
    [ -n "$IDENTITY" ] || { echo "!! encrypted archive: pass the private key with -i <file>" >&2; exit 1; }
    echo "    decrypting"
    age -d -i "$IDENTITY" -o "$WORK/archive.tar.gz" "$ARCHIVE"
    TARBALL="$WORK/archive.tar.gz"
fi
mkdir -p "$WORK/restore"
tar -xzf "$TARBALL" -C "$WORK/restore"
R="$WORK/restore"

problems=0
# `set -e` aborts on a bare failing test, so conditions go through this.
has() { if "$@"; then echo 1; else echo 0; fi; }
check() {  # check <ok?> <message>
    if [ "$1" = 1 ]; then echo "    ok   $2"; else echo "    FAIL $2"; problems=$((problems + 1)); fi
}
check "$(has test -s "$R/database.sql")" "database.sql present"
check "$(has test -f "$R/config.yaml")" "configs/config.yaml present"
yamls=$(find "$R/configs" -maxdepth 1 -name '*.yaml' 2>/dev/null | wc -l)
check "$(has test "$yamls" -gt 0)" "$yamls workspace config(s), accounts.json $( [ -f "$R/configs/accounts.json" ] && echo present || echo MISSING)"
products=$(find "$R/accounts" -name product.json 2>/dev/null | wc -l)
echo "    info $products product(s) in the catalogue; .env $( [ -f "$R/env" ] && echo included || echo 'not included (unencrypted archive)')"

# -- database ---------------------------------------------------------------
echo "    restoring into throwaway database $DRILL_DB"
DB_START=$(date +%s)
psql_in postgres -q -c "CREATE DATABASE $DRILL_DB;" >/dev/null
if psql_in "$DRILL_DB" -q < "$R/database.sql" > "$WORK/restore.log" 2>&1; then
    check 1 "database restored in $(( $(date +%s) - DB_START ))s"
else
    check 0 "database restore failed - see below"
    tail -n 5 "$WORK/restore.log" | sed 's/^/         /'
fi

count() {  # count <database> <table>
    psql_in "$1" -tAq -c "SELECT count(*) FROM $2" 2>/dev/null || echo "-"
}
migrations=$(psql_in "$DRILL_DB" -tAq -c "SELECT count(*) FROM schema_migrations" 2>/dev/null || echo 0)
check "$(has test "${migrations:-0}" -gt 0)" "schema_migrations: $migrations version(s)"

printf '    %-24s %12s %12s\n' "table" "restored" "live now"
for table in accounts conversations conversation_messages customer_profile tasks message_escalations notifications; do
    printf '    %-24s %12s %12s\n' "$table" "$(count "$DRILL_DB" "$table")" "$(count "$POSTGRES_DB" "$table")"
done
echo "    (live is usually a little ahead: it kept running after the backup)"

echo "==> $( [ "$problems" -eq 0 ] && echo PASSED || echo "FAILED ($problems problem(s))") in $(( $(date +%s) - START ))s total"
[ "$problems" -eq 0 ]
