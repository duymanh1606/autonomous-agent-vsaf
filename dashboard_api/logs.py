"""Tail a workspace's log files for the admin "Log" page.

Every process a workspace runs writes under
`data/accounts/<id>/logs/<name>.log` (see `processes.AccountProcesses`), and
`runtime.log` alone reaches tens of MB, so this reads backwards from the end
of the file in blocks and stops as soon as it has enough entries - it never
loads a whole log into memory.

An "entry" is one logging record: a line starting with the
`logging` timestamp plus every following line without one (a traceback, a
multi-line uvicorn error). Filtering by level therefore keeps a traceback
together with the ERROR line that introduced it instead of dropping it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# The only files this may open. The name from the request is looked up here,
# never joined onto a path, so it cannot point anywhere else on disk.
LOG_FILES: dict[str, str] = {
    "runtime": "runtime.log",
    "chrome": "chrome.log",
    "xvfb": "xvfb.log",
    "x11vnc": "x11vnc.log",
    "novnc": "novnc.log",
}

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_RANK = {name: rank for rank, name in enumerate(LEVELS)}

# `%(asctime)s %(levelname)s %(name)s: %(message)s` - main.py's log format.
_HEADER = re.compile(
    rb"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
)

_BLOCK = 64 * 1024
# How far back one request may scan looking for matches. A filter that
# matches nothing would otherwise read the whole file on every refresh.
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 2000


def available_files(log_dir: Path) -> list[dict[str, Any]]:
    files = []
    for key, filename in LOG_FILES.items():
        path = log_dir / filename
        if path.is_file():
            stat = path.stat()
            files.append({"name": key, "size": stat.st_size, "modified": stat.st_mtime})
    return files


def _matches(entry: dict[str, Any], min_rank: int, needle: str) -> bool:
    if min_rank > 0:
        level = entry["level"]
        if level is None or _LEVEL_RANK[level] < min_rank:
            return False
    return not needle or needle in entry["text"].casefold()


def tail(
    path: Path,
    *,
    limit: int = 500,
    min_level: str | None = None,
    query: str = "",
    max_scan_bytes: int = MAX_SCAN_BYTES,
) -> dict[str, Any]:
    """Return the last `limit` matching entries of `path`, oldest first.

    `truncated` is true when the scan stopped at `max_scan_bytes` before the
    start of the file - older matches may exist that were not looked at.
    """
    limit = max(1, min(limit, MAX_ENTRIES))
    min_rank = _LEVEL_RANK.get((min_level or "").upper(), 0)
    needle = query.strip().casefold()

    found: list[dict[str, Any]] = []
    # Continuation lines seen so far (newest first) that still wait for the
    # header line above them.
    pending: list[bytes] = []
    scanned = 0

    def close_entry(header: bytes | None) -> None:
        lines = ([header] if header is not None else []) + pending[::-1]
        pending.clear()
        if not lines:
            return
        level_match = _HEADER.match(header) if header is not None else None
        entry = {
            "level": level_match.group(1).decode() if level_match else None,
            "text": b"\n".join(lines).decode("utf-8", errors="replace"),
        }
        if _matches(entry, min_rank, needle):
            found.append(entry)

    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        size = position
        carry = b""
        while position > 0 and len(found) < limit and scanned < max_scan_bytes:
            step = min(_BLOCK, position)
            position -= step
            handle.seek(position)
            chunk = handle.read(step) + carry
            scanned += step
            lines = chunk.split(b"\n")
            # The first piece may be the tail end of a line that starts in
            # the previous block; keep it for the next round unless this is
            # the start of the file.
            carry = lines.pop(0) if position > 0 else b""
            for line in reversed(lines):
                if not line.strip() and not pending:
                    continue
                if _HEADER.match(line):
                    close_entry(line)
                    if len(found) >= limit:
                        break
                else:
                    pending.append(line)
        if len(found) < limit and position == 0:
            # Lines above the first timestamped record (Python warnings
            # printed at import, say) form one entry of their own.
            close_entry(None)

    found.reverse()
    return {
        "entries": found[-limit:],
        "size": size,
        "truncated": position > 0 and len(found) < limit,
    }
