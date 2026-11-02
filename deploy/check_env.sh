#!/usr/bin/env bash
# Refuse a .env that `source .env` in deploy.sh would misread, before it is
# sourced.
#
# Why this exists: deploy.sh sources .env as bash, and bash reads
# `KEY=abcd efgh ijkl mnop` (an unquoted Gmail app password, say) as "run the
# command `efgh` with KEY set" - not as a value with spaces. Under `set -e` that
# is a silent death with only `efgh: command not found` in the journal, and
# systemd retries it every 5 seconds forever while nginx answers 502. Worse,
# had the word been the name of a real command, `source` would have RUN it.
# `bash -n` cannot catch any of this: the line is perfectly valid syntax.
#
# So each line is checked by shape, never executed. Only shapes that make
# `source` do something other than assign are rejected - a value that sources
# fine today must keep passing, or this check would take down a working
# deployment. Values are never printed: this output ends up in the journal.
#
# Usage: deploy/check_env.sh [path/to/.env]     (exit 0 = OK, 1 = fix it)
set -uo pipefail

ENV_FILE="${1:-.env}"
[ -f "$ENV_FILE" ] || exit 0

# Kept in variables: bash only reads these as regexes unquoted on the right of
# =~, and every shell metacharacter in them would need escaping inline.
assign_re='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
# `  # trailing comment` after a value is fine for bash.
tail_re='([[:space:]]+#.*)?[[:space:]]*$'
single_re="^'[^']*'${tail_re}"
double_re='^"([^"\\]|\\.)*"'"${tail_re}"
bare_re="^[^[:space:];&|<>()\`\"']*${tail_re}"

errors=0
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue

    if [[ ! "$line" =~ $assign_re ]]; then
        echo "!! $ENV_FILE dòng $lineno: không phải dạng TEN=giá_trị" >&2
        errors=$((errors + 1))
        continue
    fi
    name="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"

    if [[ "$value" =~ $single_re || "$value" =~ $double_re || "$value" =~ $bare_re ]]; then
        continue
    fi
    echo "!! $ENV_FILE dòng $lineno ($name): giá trị có dấu cách, dấu nháy lẻ" \
         "hoặc ký tự shell (; & | < > ( ) \`) mà không nằm trong nháy." >&2
    echo "   Sửa: đặt giá trị trong nháy đơn, ví dụ $name='giá trị có dấu cách'" >&2
    errors=$((errors + 1))
done < "$ENV_FILE"

if [ "$errors" -gt 0 ]; then
    echo "!! $ENV_FILE có $errors dòng sai - chưa khởi động gì cả. Sửa xong rồi chạy lại" \
         "(systemd: sudo systemctl restart autonomous-agent)." >&2
    exit 1
fi
exit 0
