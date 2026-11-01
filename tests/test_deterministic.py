"""Tests for the deterministic pieces: validators, normalization, evidence checks, name matching."""
from app.extract.evidence import excerpt_in_source, score, value_in_excerpt
from app.extract.normalize import names_compatible, normalize, parse_date, vin_check
from app.classify import classify_text_rules


def test_vin_check_digit_and_length():
    assert vin_check("4V4NC9EH9LN248615") == {"length_17": True, "no_IOQ": True, "check_digit": True}
    assert vin_check("3AKJGLD59HSKR736")["length_17"] is False          # the 16-char VIN in the sample
    assert vin_check("4V4NC9EH5LN248615")["check_digit"] is False       # one digit changed


def test_dates_in_sample_formats():
    assert parse_date("09/14/79").isoformat() == "1979-09-14"
    assert parse_date("12/15/2026.").isoformat() == "2026-12-15"         # trailing period in the app
    assert parse_date("September 30, 2025").isoformat() == "2025-09-30"
    assert parse_date("06-02-1983").isoformat() == "1983-06-02"


def test_names_entity_suffix_is_a_conflict_but_middle_initial_is_not():
    assert not names_compatible("COBALT MESA TRANSPORT INC", "COBALT MESA TRANSPORT LLC")
    assert names_compatible("WHITFIELD, MARCUS J", "MARCUS J WHITFIELD")
    assert names_compatible("ALEKSANDR P VOLKOV", "VOLKOV ALEKSANDR PAVEL")
    assert not names_compatible("KEVIN BRANDT", "MARCUS J WHITFIELD")


def test_money_and_bool_normalization():
    assert normalize("al_limit", "$ 1,000,000") == 1_000_000
    assert normalize("total", "-$326.30") == -326.30
    assert normalize("hazmat", "No") is False
    assert normalize("al_deductible", "Do not offer") == "DO NOT OFFER"


def test_evidence_must_exist_in_source_and_contain_value():
    page = "Policy Number:GAL8100457 Policy Period:12/15/2024 to 12/15/2025"
    assert excerpt_in_source("Policy Period:12/15/2024 to 12/15/2025", page)
    assert not excerpt_in_source("Policy Period:12/15/2023 to 12/15/2024", page)
    assert value_in_excerpt("term_start", "12/15/2024", "Policy Period:12/15/2024 to 12/15/2025")
    assert value_in_excerpt("term_start", "12/15/2023", "Policy Period:12/15/2024 to 12/15/2025") is False


def test_confidence_drops_for_unverified_evidence_and_scans():
    page = "DOT # 9481307 MC # 1987405"
    good, _ = score("llm_text", "mc_number", "1987405", "MC # 1987405", page, {})
    fabricated, _ = score("llm_text", "mc_number", "1987405", "MC# 1987405 (from FMCSA)", page, {})
    scan, _ = score("llm_vision", "mc_number", "1987405", "MC # 1987405", page, {})
    assert good >= 0.85 > scan and fabricated < 0.5


def test_classifier_uses_content_not_file_name():
    mvr = "CALIFORNIA Driver Record - K2291 Violations/Convictions Failures To Appear License and Permit Information"
    assert classify_text_rules(mvr)[0] == "MVR"
    assert classify_text_rules("END OF REPORT FOR SANDHU, DEVRAJ (CONTROL NUMBER: X)")[0] == "CONTINUATION"
    assert classify_text_rules("Dear sir, please find our renewal letter") is None   # goes to the LLM tier


def test_state_names_map_to_codes_without_guessing():
    from app.extract.normalize import state_code
    assert state_code("California") == "CA"
    assert state_code("west virginia") == "WV"
    assert state_code("NJ") == "NJ"
    assert state_code("Ontario") is None
    assert normalize("jurisdiction", "New Mexico") == "NM"


def test_formula_cells_without_saved_value_are_reported_not_recomputed():
    from types import SimpleNamespace
    from app.intake import cell_text
    from app.extract.xlsx_extract import _fact
    cell = SimpleNamespace(coordinate="F7", value="=SUM(F2:F6)")
    assert "no calculated value saved" in cell_text(cell, None)
    assert _fact("vehicles", "vehicle", 0, "cost_new", cell, None) is None
    assert _fact("vehicles", "vehicle", 0, "cost_new", cell, 193000)["value"] == "193000"


def test_stale_mvr_is_superseded_and_kept():
    from app.conflicts import _duplicate_mvrs
    from app.db import Field
    def f(fid, page, name, value):
        return Field(id=fid, page_id=page, doc_type="MVR", entity_type="driver", entity_key="driver:W1",
                     name=name, value=value, method="llm_text", status="OK", validation={})
    new_date, new_lic = f(1, 10, "mvr_order_date", "10/09/2026"), f(2, 10, "license_status", "VALID")
    old_date, old_viol = f(3, 20, "mvr_order_date", "09/24/2025"), f(4, 20, "points", "4")
    conflicts = _duplicate_mvrs(1, [old_date, old_viol, new_date, new_lic])
    assert conflicts[0].status == "RESOLVED" and conflicts[0].suggested_field_id == 1
    assert old_date.status == old_viol.status == "SUPERSEDED"
    assert new_date.status == new_lic.status == "OK"
