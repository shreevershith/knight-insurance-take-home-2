"""D-31: IFTA tables read by code, per-task model routing, escalation only with a cheap tier."""
from app import config, llm
from app.extract.table_transcript import parse_ifta_page
from app.resolve import assign_keys

PAGE_2 = """Account Number: 247-918305
Filing Period: March 31, 2026
Jurisdiction Summary
Jurisdiction | Surcharge | Fuel Type | Total Miles | Taxable Miles | MPG | Taxable Gal | Tax Paid Gal | Net Taxable Gal | Rate | Tax | Interest | Total
Alabama | | 2. Diesel | 448 | 448 | 6.05 | 74 | 39 | 35 | 0.2900 | $10.15 | $0.23 | $10.38
Kentucky | | 2. Diesel | 1,659 | 1,659 | 6.05 | 274 | 40 | 234 | 0.2200 | $51.48 | $1.16 | $52.64
Kentucky | Surcharge | 2. Diesel | 0 | 0 | 0.00 | 274 | 0 | 0 | 0.1050 | $28.77 | $0.65 | $29.42"""

PAGE_3 = """Account Number: 247-918305
Filing Period: March 31, 2026
Texas | 2. Diesel | 841 | 841 | 6.05 | 139 | 1,220 | -1,081 | 0.2000 | -$216.20 | $0.00 | -$216.20
2,948 | 2,948 | 761 | 1,299 | 0 | $3,464.20 | $134.80 | $3,599.00"""


def _by_key(facts):
    out = {}
    for f in facts:
        out.setdefault(f["entity_key"], {})[f["field"]] = f["value"]
    return out


def test_ifta_table_parsed_in_code_across_continuation_page():
    ctx = {"doc_id": 1, "ifta_period": "2026-03-31"}
    p2, p3 = parse_ifta_page(PAGE_2, ctx), parse_ifta_page(PAGE_3, ctx)
    assign_keys(p2, ctx, [])
    assign_keys(p3, ctx, [])
    rows = _by_key(p2 + p3)
    assert rows["ifta_row:2026-03-31:KY"]["total_miles"] == "1,659"
    assert rows["ifta_row:2026-03-31:KY-SURCHARGE"]["total_miles"] == "0"   # surcharge kept apart
    assert rows["ifta_row:2026-03-31:TX"]["total_miles"] == "841"          # header carried, column dropped
    assert rows["ifta:2026-03-31"]["total_miles"] == "2,948"               # the total row
    for f in p2 + p3:
        assert f["value"] in f["excerpt"]                                   # excerpt = the row line


def test_ifta_parser_gives_up_rather_than_guess():
    assert parse_ifta_page("Taxpayer Name: X\n1. Penalty | $0.00", {"doc_id": 1}) is None
    bad = PAGE_2 + "\nAlberta | | 2. Diesel | 10 | 10 | 6 | 1 | 1 | 0 | 0.1 | $1 | $0 | $1"
    assert parse_ifta_page(bad, {"doc_id": 1}) is None                     # a province: the LLM decides


def test_routing_defaults_and_overrides(monkeypatch):
    # D-31 defaults: model per task
    assert config.route("classify")[0] == "claude-haiku-5-5"
    assert config.route("transcribe")[0] == "claude-sonnet-5-5"
    assert config.route("extract")[0] == "claude-haiku-5-5"
    assert config.route("analyst")[0] == config.CLAUDE_MODEL
    monkeypatch.setenv("LLM_MODEL_TRANSCRIBE", "claude-haiku-5-5")
    monkeypatch.setenv("LLM_EFFORT_TRANSCRIBE", "low")
    assert config.route("transcribe") == ("claude-haiku-5-5", "low")
    assert not config.supports_fallbacks("claude-haiku-5-5")
    assert llm.tier_for("classify_vision") == "transcribe"
    assert llm.tier_for("extract_mvr") == "extract"
    assert round(config.cost_usd("claude-opus-5-5", 1_000_000, 100_000), 2) == 6.0


def test_full_width_total_row_reads_the_total_miles_column():
    page = PAGE_2.split("\nAlabama")[0] + "\nAlabama | | 2. Diesel | 448 | 448 | 6.05 | 74 | 39 | 35 | 0.29 | $1 | $0 | $1\n | | | 448 | 448 | | 74 | 39 | 35 | | $1 | $0 | $1"
    facts = parse_ifta_page(page, {"doc_id": 1})
    assert [f["value"] for f in facts if f["field"] == "total_miles" and f["entity_type"] == "ifta"] == ["448"]


def test_continuation_page_is_extracted_as_the_document_it_continues():
    from types import SimpleNamespace as NS
    from app.pipeline import _inherit_continuations
    table = NS(doc_type="CONTINUATION", classify_method="llm_vision", classify_reason="tail rows")
    trailer = NS(doc_type="CONTINUATION", classify_method="rules", classify_reason="END OF REPORT")
    sub = NS(documents=[NS(pages=[NS(doc_type="IFTA_RETURN", classify_method="llm_vision", classify_reason=""), table]),
                        NS(pages=[NS(doc_type="MVR", classify_method="rules", classify_reason=""), trailer])])
    _inherit_continuations(sub)
    assert table.doc_type == "IFTA_RETURN"      # rows + total get extracted
    assert trailer.doc_type == "CONTINUATION"   # nothing to extract on a report trailer


def test_ifta_parser_tolerates_layout_variants_between_models():
    # seen in real transcripts: Sonnet pipes key/value lines, Haiku labels the total row "Totals"
    ctx = {"doc_id": 1, "ifta_period": "2026-03-31"}
    parse_ifta_page(PAGE_2, ctx)
    page = ("Account Number: | 247-918305\nFiling Period: | March 31, 2026\n"
            "Texas | 2. Diesel | 841 | 841 | 6.05 | 139 | 1,220 | -1,081 | 0.2000 | -$216.20 | $0.00 | -$216.20\n"
            "Totals | | 2,948 | 2,948 | | 761 | 1,299 | 0 | | $3,464.20 | $134.80 | $3,599.00")
    facts = parse_ifta_page(page, ctx)
    assert [f["value"] for f in facts if f["field"] == "total_miles" and f["entity_type"] == "ifta"] == ["2,948"]
    assert sum(f["entity_type"] == "ifta_row" and f["field"] == "jurisdiction" for f in facts) == 1


def test_ifta_parser_declines_a_page_run_together_on_one_line():
    one_line = " | ".join(PAGE_2.splitlines())  # Haiku once returned a whole page as one line
    assert parse_ifta_page(one_line, {"doc_id": 1}) is None


def test_each_return_in_one_pdf_keys_to_its_own_quarter():
    # held-out #2: four returns in one PDF; a page that names its quarter must not inherit the previous one
    ctx = {"doc_id": 1}
    q4 = "Quarter: 10/01/2025 through 12/31/2025   Due: 01/31/2026\n" + PAGE_2.split("\n", 2)[2]
    q1 = q4.replace("10/01/2025 through 12/31/2025   Due: 01/31/2026", "01/01/2026 through 03/31/2026   Due: 04/30/2026")
    first, second = parse_ifta_page(q4, ctx), parse_ifta_page(q1, ctx)
    assign_keys(first, ctx, [])
    assign_keys(second, ctx, [])
    assert {f["entity_key"].split(":")[1] for f in first if f["entity_type"] == "ifta_row"} == {"2025-12-31"}
    assert {f["entity_key"].split(":")[1] for f in second if f["entity_type"] == "ifta_row"} == {"2026-03-31"}
