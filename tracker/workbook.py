"""Read / append / format the single-sheet Events workbook."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .extract import Event

SHEET = "Events"
COLUMNS = [
    ("Name", 42), ("Date", 13), ("End Date", 13), ("Organiser/Host", 22), ("Region", 12),
    ("City", 18), ("Format", 11), ("Companies Attending", 28), ("Role", 18),
    ("Named Speakers", 30), ("Cost", 10), ("URL", 45), ("Source", 26),
    ("Date Added", 12), ("Status", 12), ("Notes", 30),
]
HEADERS = [c for c, _ in COLUMNS]
IDX = {h: i for i, h in enumerate(HEADERS)}
STATUSES = ["New", "Attending", "Skip"]
MAX_ROWS = 5000


def event_key(name: str, start: date, city: str) -> str:
    """Dedupe key: normalised name + start date + city."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return hashlib.sha1(f"{norm(name)}|{start.isoformat()}|{norm(city)}".encode()).hexdigest()


def _to_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


def load_rows(path: Path) -> list[list]:
    """Existing data rows as lists in HEADERS order ([] if the file does not exist)."""
    if not path.exists():
        return []
    ws = load_workbook(path)[SHEET]
    rows = []
    for r in ws.iter_rows(min_row=2, max_col=len(HEADERS), values_only=True):
        if any(v not in (None, "") for v in r):
            rows.append(list(r) + [None] * (len(HEADERS) - len(r)))
    return rows


def row_key(row: list) -> str | None:
    start = _to_date(row[IDX["Date"]])
    return event_key(row[IDX["Name"]], start, row[IDX["City"]]) if start and row[IDX["Name"]] else None


def event_to_row(ev: Event, added: date) -> list:
    return [
        ev.name, ev.start, ev.end, ev.host, ev.region, ev.city, ev.fmt,
        ", ".join(ev.companies), ev.role, ev.speakers, ev.cost, ev.url, ev.source,
        added, "New", "",
    ]


def _norm(name) -> str:
    n = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower())
    return re.sub(r"\b20\d\d\b", "", n).strip()


def _same_event(a: tuple[date, str], b: tuple[date, str]) -> bool:
    """Start dates within 3 days (sources disagree by a day) and one normalised name is a prefix of the other (e.g. '... 2027' vs '... 2027, co-located with ...')."""
    (da, na), (db, nb) = a, b
    if not da or not db or abs((da - db).days) > 3 or not na or not nb:
        return False
    short, long_ = sorted((na, nb), key=len)
    return len(short) >= 8 and long_.startswith(short)


def merge(path: Path, events: list[Event], today: date | None = None) -> tuple[int, int]:
    """Append events not already present; keep existing rows untouched. Returns (added, total)."""
    today = today or date.today()
    rows = load_rows(path)
    seen = {k for k in map(row_key, rows) if k}
    by_date = [(_to_date(r[IDX["Date"]]), _norm(r[IDX["Name"]])) for r in rows]
    added = 0
    for ev in events:
        k = event_key(ev.name, ev.start, ev.city)
        sig = (ev.start, _norm(ev.name))
        if k in seen or any(_same_event(sig, existing) for existing in by_date):
            continue
        seen.add(k)
        by_date.append(sig)
        rows.append(event_to_row(ev, today))
        added += 1
    rows.sort(key=lambda r: (_to_date(r[IDX["Date"]]) or date.max, str(r[IDX["Name"]] or "")))
    write(path, rows)
    return added, len(rows)


def write(path: Path, rows: list[list]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(HEADERS)
    for r in rows:
        ws.append(r)

    head_fill = PatternFill("solid", fgColor="1F3864")
    for i, (_, width) in enumerate(COLUMNS, start=1):
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    for col in ("Date", "End Date", "Date Added"):
        for cell in ws[get_column_letter(IDX[col] + 1)][1:]:
            cell.number_format = "yyyy-mm-dd"
    url_col = IDX["URL"] + 1
    for row in range(2, len(rows) + 2):
        cell = ws.cell(row=row, column=url_col)
        if isinstance(cell.value, str) and cell.value.startswith("http"):
            cell.hyperlink = cell.value
            cell.font = Font(color="0563C1", underline="single")

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{max(len(rows) + 1, 2)}"
    status_col = get_column_letter(IDX["Status"] + 1)
    dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=True)
    dv.add(f"{status_col}2:{status_col}{MAX_ROWS}")
    ws.add_data_validation(dv)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
