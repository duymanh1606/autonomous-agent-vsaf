"""Loader for the Burmese customer-care dataset (data/testcase-myanmar/*.xlsx).

The workbook is not committed (`/data/*` is gitignored), so callers skip when
it is absent. Read with the standard library rather than openpyxl, which is
not a project dependency: an .xlsx is a zip of XML, and this one sheet only
needs its cell strings - inline as exported, or shared once re-saved in Excel.

Columns: test_id, intent, intent_vi, customer_message_mm, expected_reply_mm,
expected_reply_vi, notes. The notes say it is synthetic and not yet reviewed
by a native speaker, so the expected replies are a reference, not a gold
standard to compare model output against.
"""

import os
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = BASE_DIR / "data" / "testcase-myanmar"

_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass(frozen=True)
class MyanmarCase:
    test_id: str
    intent: str
    intent_vi: str
    customer_message: str
    expected_reply_mm: str
    expected_reply_vi: str


def dataset_path() -> Path | None:
    """`MYANMAR_DATASET` if set, else the one workbook in DATASET_DIR."""
    override = os.environ.get("MYANMAR_DATASET")
    if override:
        return Path(override)
    found = sorted(DATASET_DIR.glob("*.xlsx"))
    return found[0] if found else None


def _text(node) -> str:
    return "".join(part.text or "" for part in node.iter(f"{{{_NS['x']}}}t"))


def _column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference).group(0)
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def load_cases(path: Path | None = None) -> list[MyanmarCase]:
    path = path or dataset_path()
    if path is None or not path.exists():
        return []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [_text(item) for item in root.findall("x:si", _NS)]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows = []
    for row in sheet.findall("x:sheetData/x:row", _NS):
        values = {}
        for cell in row.findall("x:c", _NS):
            kind = cell.get("t")
            if kind == "inlineStr":
                value = _text(cell)
            elif kind == "s":
                value = shared[int(cell.find("x:v", _NS).text)]
            else:
                found = cell.find("x:v", _NS)
                value = found.text if found is not None else ""
            values[_column(cell.get("r"))] = (value or "").strip()
        rows.append([values.get(index, "") for index in range(7)])

    header, body = rows[0], rows[1:]
    expected = [
        "test_id", "intent", "intent_vi", "customer_message_mm",
        "expected_reply_mm", "expected_reply_vi",
    ]
    if header[:6] != expected:
        raise ValueError(f"unexpected columns in {path.name}: {header}")
    return [MyanmarCase(*row[:6]) for row in body if row[0]]
