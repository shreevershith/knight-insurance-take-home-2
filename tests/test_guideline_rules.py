"""The remaining guideline sections: each rule's deciding case, built from synthetic values."""
import pytest

from app.db import Document, Field, Page, RuleResult, SessionLocal, Submission, init_db
from app.rules import engine

APP = [("APPLICATION", "applicant", "application_date", "10/22/2026"),
       ("APPLICATION", "applicant", "mailing_state", "CA")]


def _evaluate(db, rows):
    """rows: (doc_type, entity_key, field, value). Returns {rule_id: RuleResult} and the submission."""
    s = Submission(subject="guideline")
    db.add(s)
    db.flush()
    pages = {}
    for doc_type in {r[0] for r in rows}:
        d = Document(submission_id=s.id, filename=f"{doc_type}.pdf", sha256=f"{s.id}{doc_type}", blob_path="x", status="OK")
        db.add(d)
        db.flush()
        pages[doc_type] = Page(document_id=d.id, page_no=1, doc_type=doc_type, status="OK", text="")
        db.add(pages[doc_type])
    db.flush()
    for doc_type, key, name, value in rows:
        db.add(Field(submission_id=s.id, page_id=pages[doc_type].id, doc_type=doc_type, entity_type=key.split(":")[0],
                     entity_key=key, name=name, value=value, method="llm_text", confidence=0.9, status="OK"))
    db.commit()
    engine.evaluate(db, s)
    db.commit()
    last = db.query(RuleResult).filter_by(submission_id=s.id).order_by(RuleResult.id.desc()).first()
    return {r.rule_id: r for r in db.query(RuleResult).filter_by(submission_id=s.id, run_id=last.run_id)}, s


def _driver(key="driver:D1", **mvr):
    rows = [("DRIVER_SCHEDULE", key, "full_name", "DOE, JANE"), ("DRIVER_SCHEDULE", key, "dob", "01/01/1980"),
            ("MVR", key, "license_status", "VALID"), ("MVR", key, "license_class", "A"), ("MVR", key, "mvr_order_date", "10/01/2026")]
    return rows + [("MVR", key, k, v) for k, v in mvr.items()]


def _violation(i, desc, when, points="", key="driver:D1"):
    vk = f"violation:{key}:{i}"
    rows = [("MVR", vk, "description", desc), ("MVR", vk, "violation_date", when)]
    return rows + ([("MVR", vk, "points", points)] if points else [])


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def test_recent_dui_declines_older_one_does_not(db):
    r, _ = _evaluate(db, APP + _driver() + _violation(0, "DUI - ALCOHOL", "03/01/2024"))
    assert r["DRV-07"].outcome == "DECLINE"
    r, _ = _evaluate(db, APP + _driver() + _violation(0, "DUI - ALCOHOL", "03/01/2019"))
    assert r["DRV-07"].outcome == "PASS" and "1 older" in r["DRV-07"].message


def test_speeding_over_30_declines_and_a_range_that_may_exceed_is_not_guessed(db):
    r, _ = _evaluate(db, APP + _driver() + _violation(0, "SPEEDING 31-40 MPH OVER LIMIT", "05/05/2025"))
    assert r["DRV-08"].outcome == "DECLINE"
    r, _ = _evaluate(db, APP + _driver() + _violation(0, "SPEEDING 15-29 MPH OVER LIMIT", "05/05/2025"))
    assert r["DRV-08"].outcome == "PASS"
    r, _ = _evaluate(db, APP + _driver() + _violation(0, "SPEEDING 26-35 MPH OVER LIMIT", "05/05/2025"))
    assert r["DRV-08"].outcome == "NOT_EVALUATED"          # may or may not be over 30: a person checks
    r, _ = _evaluate(db, APP + _driver() + _violation(0, "RECKLESS DRIVING", "05/05/2015"))
    assert r["DRV-08"].outcome == "DECLINE"                # no lookback for this list (AMB-9)


def test_points_count_only_inside_the_windows(db):
    rows = APP + _driver() + _violation(0, "SPEEDING", "06/07/2023", "4") + _violation(1, "LANE", "01/10/2026", "3")
    r, _ = _evaluate(db, rows)
    assert r["DRV-06"].outcome == "PASS" and "3 points in 36 months" in r["DRV-06"].message
    rows += _violation(2, "SIGNAL", "02/10/2026", "2")
    r, _ = _evaluate(db, rows)
    assert r["DRV-06"].outcome == "REFER"                  # 5 points in 12 months > 4


def test_vehicle_types(db):
    def unit(vin, model):
        return [("VEHICLE_SCHEDULE", f"vehicle:{vin}", "vin", vin), ("VEHICLE_SCHEDULE", f"vehicle:{vin}", "model", model)]
    r, _ = _evaluate(db, APP + unit("1XPBD49X5MD735204", "TRACTOR") + unit("3AKJGLD59HSKR7360", "DUMP TRUCK"))
    assert r["APP-02"].outcome == "DECLINE" and r["APP-01"].outcome == "REFER"
    r, _ = _evaluate(db, APP + unit("1XPBD49X5MD735204", "BOX TRUCK"))
    assert r["APP-02"].outcome == "PASS" and r["SEL-02"].outcome == "REFER"   # AMB-1: selective, not declined


def test_prior_insurance_gap_and_missing_loss_run(db):
    def term(start, end):
        k = f"prior_policy:{start}"
        return [("QUOTE_FORM", k, "term_start", start), ("QUOTE_FORM", k, "term_end", end)]
    rows = APP + term("03/08/2022", "02/10/2023") + term("12/15/2023", "12/15/2024") + term("12/15/2024", "12/15/2025") \
        + term("12/15/2025", "12/15/2026")
    rows += [("LOSS_RUN", "loss_run:AL:P1", "term_start", "12/15/2023"), ("LOSS_RUN", "loss_run:AL:P1", "term_end", "12/15/2024")]
    r, _ = _evaluate(db, rows)
    assert r["PRI-01"].outcome == "REFER" and "54 days" in r["PRI-01"].message   # 10/22/2023 -> 12/15/2023
    assert r["PRI-02"].outcome == "REFER" and "308 days" in r["PRI-02"].message
    assert r["PRI-03"].outcome == "REFER" and "03/08/2022" in r["PRI-03"].message


def test_ifta_window_is_quarters_due_by_the_application_date(db):
    rows = APP + [("IFTA_RETURN", f"ifta:{p}", "period_end", p) for p in ("2025-12-31", "2026-03-31", "2026-06-30")]
    r, _ = _evaluate(db, rows)
    assert r["SUB-05"].outcome == "REFER" and "Q3 2025" in r["SUB-05"].message   # Q3 2026 isn't due until 10/31


def test_new_venture_corporation_refers(db):
    rows = APP + [("APPLICATION", "applicant", "years_in_business", "1"), ("APPLICATION", "applicant", "entity_type", "Corporation")]
    r, _ = _evaluate(db, rows)
    assert r["NV-02"].outcome == "REFER" and r["NV-01"].outcome == "PASS"


def test_apd_request_is_a_note_not_a_referral(db):
    r, s = _evaluate(db, APP + [("QUOTE_FORM", "coverage", "apd_requested", "yes")])
    assert r["COV-02"].outcome == "FLAG"


def test_no_power_units_means_no_team(db):
    r, s = _evaluate(db, APP)
    assert r["RTE-01"].outcome == "NOT_EVALUATED" and s.team == ""


def test_state_read_from_a_one_line_address(db):
    rows = [("APPLICATION", "applicant", "application_date", "10/22/2026"),
            ("APPLICATION", "applicant", "garaging_address", "418 Industrial Pkwy, Jackson, TN 38301")]
    r, _ = _evaluate(db, rows)
    assert r["ST-01"].outcome == "PASS" and "TN" in r["ST-01"].message and r["ST-03"].outcome == "PASS"


def test_explicit_no_to_towing_passes_even_with_an_incomplete_commodity_list(db):
    rows = APP + [("APPLICATION", "operations", "towing", "no"),
                  ("APPLICATION", "commodity:c:0", "commodity", "Lumber"), ("APPLICATION", "commodity:c:0", "percent", "60")]
    r, _ = _evaluate(db, rows)
    assert r["PRO-06"].outcome == "PASS" and r["PRO-02"].outcome == "NOT_EVALUATED"
    rows[-2] = ("APPLICATION", "commodity:c:0", "commodity", "Wrecker / towing services")
    r, _ = _evaluate(db, rows)
    assert r["PRO-06"].outcome == "DECLINE"   # a towing commodity outweighs the No
