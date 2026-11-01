"""Deterministic parsing of tables in a vision transcript (D-31).

The vision step already writes tables as one row per line with ' | ' between cells. Reading those
rows is arithmetic-free bookkeeping, so code does it: columns are mapped by header synonyms (like the
XLSX schedules), each value's excerpt is its row line, and the usual evidence check applies.
The transcription itself is still model output, so values keep the vision prior until the rows
reconcile to the printed total (pipeline._ifta_reconciliation).

Returns None whenever the page doesn't parse cleanly; the caller then uses the LLM extractor.
A row the parser skips (or misreads) can't pass silently: the rows must add up to the printed total
(pipeline._ifta_reconciliation), and a return that doesn't is re-read or left LOW_CONFIDENCE.
"""
import re

from app.extract.normalize import parse_number, state_code

# Columns are matched by meaning, not exact text: transcripts of the same form differ by model
# ("Tax Paid Gal" / "Tax-Paid Gallons", "Total" / "Total Due").
IFTA_HEADERS = {
    r"^(jurisdiction|state|province)(?![a-z])": "jurisdiction", r"^surcharge": "surcharge",
    r"^(ifta )?total (ifta )?mi(les)?(?![a-z])": "total_miles", r"^taxable mi(les)?(?![a-z])": "taxable_miles",
    r"^tax[- ]?paid gal": "tax_paid_gallons", r"^total( due| amount)?$": "total",
}
_OPTIONAL = {"surcharge"}  # a column the transcript may drop when every cell in it is empty
_TOTAL_LABEL = re.compile(r"^(grand )?totals?:?$", re.I)
_MAX_COLUMNS = 25  # a "header" wider than this is a whole page run together on one line: give up


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split("|")]


def _header(cells: list[str]) -> list[str | None] | None:
    mapped = []
    for c in cells:
        name = next((n for p, n in IFTA_HEADERS.items() if re.search(p, c.strip().lower())), None)
        mapped.append(name)
    if len(cells) > _MAX_COLUMNS or mapped.count("jurisdiction") != 1 or "total_miles" not in mapped:
        return None
    return mapped


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
        if len(cells) > _MAX_COLUMNS:
            return None  # rows run together: the LLM extractor handles this page
        labelled_total = bool(_TOTAL_LABEL.match(cells[0])) and all(_numeric(c) for c in cells[1:] if c)
        if labelled_total or all(_numeric(c) for c in cells if c):  # the total row: no jurisdiction
            # full width: read the Total Miles column; collapsed (empty cells dropped): first number
            value = (cells[header.index("total_miles")] if len(cells) == len(header)
                     else next((c for c in cells if c and _numeric(c)), ""))
            if value:
                facts.append({"group": "ifta", "entity_type": "ifta", "index": 0, "field": "total_miles",
                              "value": value, "excerpt": line.strip()})
            continue
        if len(cells) < len(header) - len(_OPTIONAL):
            continue  # not a table row ("Account Number: | 247-918305"); a lost real row fails reconciliation
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
