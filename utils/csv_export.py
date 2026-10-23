"""CSV generation that is safe to open in Excel / Google Sheets."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence

# Cells starting with these characters are interpreted as formulas by spreadsheet apps.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_TRIGGERS):
        return "'" + text
    return text


def build_csv(header: Sequence[str], rows: Iterable[Sequence[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([safe_cell(column) for column in header])
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    # UTF-8 with BOM so Excel on Windows shows non-ASCII usernames correctly.
    return buffer.getvalue().encode("utf-8-sig")
