"""Deterministic parsing of tables in a vision transcript (D-31).

The vision step already writes tables as one row per line with ' | ' between cells. Reading those
rows is arithmetic-free bookkeeping, so code does it: columns are mapped by header synonyms (like the
XLSX schedules), each value's excerpt is its row line, and the usual evidence check applies.
The transcription itself is still model output, so values keep the vision prior until the rows
reconcile to the printed total (pipeline._ifta_reconciliation).

Returns None whenever the page doesn't parse cleanly; the caller then uses the LLM extractor.
"""
import re

from app.extract.normalize import parse_number, state_code

IFTA_HEADERS = {
    r"^jurisdiction": "jurisdiction", r"^surcharge": "surcharge", r"^total miles": "total_miles",
    r"^taxable miles": "taxable_miles", r"^tax paid gal": "tax_paid_gallons", r"^total$": "total",
}
_OPTIONAL = {"surcharge"}  # a column the transcript may drop when every cell in it is empty


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split("|")]


def _header(cells: list[str]) -> list[str | None] | None:
    mapped = []
    for c in cells:
        name = next((n for p, n in IFTA_HEADERS.items() if re.search(p, c.strip().lower())), None)
        mapped.append(name)
    return mapped if "jurisdiction" in mapped and "total_miles" in mapped else None


def _numeric(cell: str) -> bool:
    return bool(cell) and parse_number(cell) is not None and not re.search(r"[A-Za-z]", cell)


def parse_ifta_page(text: str, page_ctx: dict) -> list[dict] | None:
    """IFTA jurisdiction table -> facts in the extractor's shape. The header row is remembered in
    page_ctx so a continuation page (no header of its own) parses the same way."""
    header = page_ctx.get("ifta_header")
    facts: list[dict] = []
    rows = 0
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = _cells(line)
        found = _header(cells)
        if found:
            header = page_ctx["ifta_header"] = found
            continue
        if header is None:
            continue
        if all(_numeric(c) for c in cells if c):  # the table's total row: no jurisdiction name
            # full width: read the Total Miles column; collapsed (empty cells dropped): first number
            value = (cells[header.index("total_miles")] if len(cells) == len(header)
                     else next((c for c in cells if c), ""))
            if value:
                facts.append({"group": "ifta", "entity_type": "ifta", "index": 0, "field": "total_miles",
                              "value": value, "excerpt": line.strip()})
            continue
        cols = header
        if len(cells) != len(cols):
            # one cell short: the transcript dropped an all-empty optional column
            drop = [i for i, c in enumerate(cols) if c in _OPTIONAL]
            if len(cells) == len(cols) - 1 and len(drop) == 1:
                cols = cols[:drop[0]] + cols[drop[0] + 1:]
            else:
                return None
        row = dict(zip(cols, cells))
        if not state_code(row.get("jurisdiction")):
            return None  # not a US jurisdiction we can key (a province, a misread): let the LLM try
        for name, value in row.items():
            if name and value:
                facts.append({"group": "jurisdictions", "entity_type": "ifta_row", "index": rows,
                              "field": name, "value": value, "excerpt": line.strip()})
        rows += 1
    return facts if rows else None
