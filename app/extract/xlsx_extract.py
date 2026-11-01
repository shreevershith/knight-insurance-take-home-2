"""Deterministic extraction for tabular spreadsheets (driver / vehicle schedules).

Columns are mapped by header synonyms, not by position, so any agency's export layout works as
long as it has a header row. Evidence is the cell reference, in the same `B3='VALUE'` form as the
page text, so it goes through the same evidence check as everything else.
"""
import re
from pathlib import Path

import openpyxl

from app.extract.normalize import looks_like_vin
from app.intake import cell_text

DRIVER_HEADERS = {
    r"^first name": "first_name", r"^middle": "middle_initial", r"^last name": "last_name",
    r"date of birth|^dob$": "dob", r"hire date|d\.?o\.?h|date of hire": "hire_date",
    r"cdl number|license ?#|license number|driver license": "license_number",
    r"cdl state|state licensed|license state": "license_state",
    r"years of experience|exp\.? in yrs|experience": "years_experience",
    r"driver type": "driver_type", r"^driver ?#$": "driver_number",
}
VEHICLE_HEADERS = {
    r"^year$": "year", r"^make$": "make", r"^model$": "model", r"^vin": "vin",
    r"vehicle type": "vehicle_type", r"gross vehicle weight|^gvw": "gvw",
    r"original new veh|cost new": "cost_new", r"stated value": "stated_value",
    r"registered state": "registered_state", r"garaged zip": "garaging_zip", r"^vehicle ?#$": "unit_number",
}
_PLACEHOLDER = re.compile(r"^(list|attached|driver|vin & vehicle|see attached|n/?a)$", re.I)


def _match(header: str, table: dict) -> str | None:
    h = header.strip().lower()
    for pattern, name in table.items():
        if re.search(pattern, h):
            return name
    return None


def _fact(group, entity_type, index, field, cell, cached=None) -> dict | None:
    """Formula cells use the calculated value saved in the file; with none saved, no value is emitted."""
    value = cell.value
    if isinstance(value, str) and value.startswith("="):
        if cached is None:
            return None
        value = cached
    return {"group": group, "entity_type": entity_type, "index": index, "field": field,
            "value": _as_text(value), "excerpt": cell_text(cell, cached)}


def _as_text(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%m/%d/%Y")
    return str(value).strip()


def extract_sheet(path: str | Path, sheet_index: int) -> list[dict]:
    ws = openpyxl.load_workbook(str(path), data_only=False).worksheets[sheet_index]
    cached_ws = openpyxl.load_workbook(str(path), data_only=True).worksheets[sheet_index]
    cached = lambda cell: cached_ws[cell.coordinate].value  # noqa: E731
    rows = list(ws.iter_rows())
    header_row, driver_cols, vehicle_cols = None, {}, {}
    for r, row in enumerate(rows):
        d = {c.column: _match(str(c.value), DRIVER_HEADERS) for c in row if isinstance(c.value, str)}
        v = {c.column: _match(str(c.value), VEHICLE_HEADERS) for c in row if isinstance(c.value, str)}
        d = {k: n for k, n in d.items() if n}
        v = {k: n for k, n in v.items() if n}
        if len(d) >= 3 or len(v) >= 3:
            header_row, driver_cols, vehicle_cols = r, d, v
            break
    if header_row is None:
        return []

    facts, notes = [], []
    n_drivers = n_vehicles = 0
    for row in rows[header_row + 1:]:
        by_col = {c.column: c for c in row if c.value not in (None, "")}
        if not by_col:
            continue
        used = set()
        drv = {name: by_col[col] for col, name in driver_cols.items() if col in by_col}
        real_driver = any(n in drv and not _PLACEHOLDER.match(str(drv[n].value).strip())
                          for n in ("last_name", "license_number"))
        if real_driver:
            for name, cell in drv.items():
                if name != "driver_number" and (fact := _fact("drivers", "driver", n_drivers, name, cell, cached(cell))):
                    facts.append(fact)
                used.add(cell.column)
            n_drivers += 1
        veh = {name: by_col[col] for col, name in vehicle_cols.items() if col in by_col}
        real_vehicle = "year" in veh or ("vin" in veh and looks_like_vin(veh["vin"].value))
        if real_vehicle:
            for name, cell in veh.items():
                if fact := _fact("vehicles", "vehicle", n_vehicles, name, cell, cached(cell)):
                    facts.append(fact)
                used.add(cell.column)
            n_vehicles += 1
        # Anything else on the sheet (totals, stray coverage notes) is kept as a note, not dropped.
        leftovers = [c for col, c in by_col.items() if col not in used]
        if leftovers and not real_driver and not real_vehicle:
            text = " ".join(cell_text(c, cached(c)) for c in leftovers)
            if not all(_PLACEHOLDER.match(str(c.value).strip()) for c in leftovers):
                notes.append((text, leftovers[0]))
    for i, (text, cell) in enumerate(notes):
        facts.append({"group": "notes", "entity_type": "sheet_note", "index": i, "field": "note",
                      "value": text, "excerpt": text})
    return facts
