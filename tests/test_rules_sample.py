"""Rules + conflicts on sample-shaped extracted values (what extraction should produce for Cobalt Mesa).
Verifies D-26..D-30 without calling the LLM. Values are the hand-checked ones in PLAN.md section 3.2."""
import pytest

from app import conflicts
from app.db import Document, Field, Page, RuleResult, SessionLocal, Submission, init_db
from app.rules import engine

APP_DATE = "10/22/2026"


@pytest.fixture()
def sub():
    init_db()
    db = SessionLocal()
    s = Submission(subject="test")
    db.add(s)
    db.flush()
    pages = {}

    def page(doc_type, name):
        if doc_type + name not in pages:
            d = Document(submission_id=s.id, filename=name, sha256=name, blob_path="x", status="OK")
            db.add(d)
            db.flush()
            p = Page(document_id=d.id, page_no=1, doc_type=doc_type, status="OK", text="")
            db.add(p)
            db.flush()
            pages[doc_type + name] = p
        return pages[doc_type + name]

    def add(doc_type, name, key, etype, **values):
        p = page(doc_type, name)
        for field, value in values.items():
            db.add(Field(submission_id=s.id, page_id=p.id, doc_type=doc_type, entity_type=etype, entity_key=key,
                         name=field, value=value, method="llm_text", confidence=0.95, status="OK"))

    add("APPLICATION", "app", "applicant", "applicant", application_date=APP_DATE, mailing_state="CA",
        dot_number="9481307", mc_number="1987405", business_name="COBALT MESA TRANSPORT INC")
    add("APPLICATION", "app", "operations", "operations", intermodal="no", cross_border="no", farthest_state="TX")
    add("QUOTE_FORM", "quote", "operations", "operations", hazmat="no")
    for i, (c, pct) in enumerate([("FRESH PRODUCE / FROZEN FOOD", "50"), ("BEER / WINE", "30"), ("PAPER & PACKAGING", "20")]):
        add("QUOTE_FORM", "quote", f"commodity:q:{i}", "commodity", commodity=c, percent=pct)
    # drivers: schedule + current MVRs (+ the photographed Osei CDL)
    roster = {"C4471925": ("DEVRAJ", "SANDHU", "09/14/1979"), "O7314062": ("NATHANIEL K", "OSEI", "02/17/1987"),
              "V61930287415503": ("ALEKSANDR P", "VOLKOV", "06/02/1983"),
              "W38215604720918": ("MARCUS J", "WHITFIELD", "03/22/1971"), "M5520814": ("LUIS E", "MORALES", "10/03/1991")}
    for lic, (first, last, dob) in roster.items():
        add("DRIVER_SCHEDULE", "drivers.xlsx", f"driver:{lic}", "driver", first_name=first, last_name=last,
            dob=dob, license_number=lic)
    add("MVR", "mvr-sandhu", "driver:C4471925", "driver", license_class="C NON-COMMERCIAL", license_status="VALID",
        license_issue_date="08/21/2025", license_expiration="09/14/2030", mvr_order_date="10/09/2026")
    add("MVR", "mvr-osei", "driver:O7314062", "driver", license_class="A COMMERCIAL", license_status="VALID",
        license_issue_date="04/09/2025", license_expiration="02/17/2030", mvr_order_date="10/13/2026")
    add("DRIVER_LICENSE", "dl-osei", "driver:O7314062", "driver", license_class="A", commercial_license="yes",
        license_issue_date="01/06/2021", license_expiration="02/17/2025")
    add("MVR", "mvr-volkov", "driver:V61930287415503", "driver", license_class="A COMB VEH>26,000 GVWR",
        license_status="VALID", license_expiration="06/02/2030", mvr_order_date="10/09/2026")
    add("MVR", "mvr-volkov", "license_event:driver:V61930287415503:0", "license_event", date="05/28/2022", event="CDL ISSUED")
    add("MVR", "mvr-volkov", "license_event:driver:V61930287415503:1", "license_event", date="10/11/2021", event="PERMIT ISSUED")
    add("MVR", "mvr-whitfield", "driver:W38215604720918", "driver", license_class="A COMB VEH>26,000 GVWR",
        license_status="VALID", license_expiration="03/22/2029", mvr_order_date="10/09/2026")
    for i, (d, e) in enumerate([("11/09/2022", "CDL CLEARANCE NOTICE"), ("10/04/2021", "CDL CLEARANCE NOTICE"),
                                ("01/20/2022", "ADDRESS CHANGE NOTICE")]):
        add("MVR", "mvr-whitfield", f"license_event:driver:W38215604720918:{i}", "license_event", date=d, event=e)
    add("MVR", "mvr-morales", "driver:M5520814", "driver", license_class="A COMMERCIAL", license_status="VALID",
        license_issue_date="06/04/2023", license_expiration="10/30/2027", first_licensed_year="2010",
        mvr_order_date="10/09/2026")
    # IFTA rows: Kentucky appears twice (surcharge row)
    for j in ["California", "Kentucky", "Texas", "New Jersey", "Arizona"]:
        add("IFTA_RETURN", "q3", f"ifta_row:2025-09-30:{j.upper()}", "ifta_row", jurisdiction=j, total_miles="1000")
    add("IFTA_RETURN", "q3", "ifta_row:2025-09-30:KENTUCKY-SURCHARGE", "ifta_row", jurisdiction="Kentucky",
        surcharge="Surcharge", total_miles="0")
    # vehicles: cost new only, one 16-character VIN
    for i, (vin, cost) in enumerate([("4V4NC9EH4NN301742", "82000"), ("4V4NC9EH9LN248615", "48000"),
                                     ("3HSDZAPR0KN617093", "41000"), ("3AKJGLD59HSKR736", "22000")]):
        add("VEHICLE_SCHEDULE", "vehicles.xlsx", f"vehicle:{vin}", "vehicle", year="2020", make="VOLVO",
            vin=vin, cost_new=cost)
    # prior terms listed on the quote vs loss runs actually received
    add("QUOTE_FORM", "quote", "prior_policy:2023-12-15", "prior_policy", term_start="12/15/2023",
        term_end="12/15/2024", carrier="GRANITE PEAK SPECIALTY", loss_runs_note="LOSS RUNS ATTACHED")
    add("QUOTE_FORM", "quote", "prior_policy:2022-03-08", "prior_policy", term_start="03/08/2022",
        term_end="02/10/2023", carrier="LAKESHORE MUTUAL CASUALTY", loss_runs_note="LOSS RUNS ATTACHED")
    add("LOSS_RUN", "al23", "loss_run:AL:GAL7102318", "loss_run", term_start="12/15/2023", term_end="12/15/2024")
    db.commit()
    yield db, s
    db.close()


def _results(db, s):
    last = db.query(RuleResult).filter_by(submission_id=s.id).order_by(RuleResult.id.desc()).first()
    return {r.rule_id: r for r in db.query(RuleResult).filter_by(submission_id=s.id, run_id=last.run_id)}


def test_cdl_experience_uses_earliest_evidence(sub):
    db, s = sub
    engine.evaluate(db, s)
    r = _results(db, s)["DRV-03"]
    assert "OSEI: CDL held since at least 01/06/2021" in r.message and "5.8 yrs" in r.message   # photo, not MVR 04/09/2025
    assert "VOLKOV: CDL held since at least 05/28/2022" in r.message and "4.4 yrs" in r.message  # 'PERMIT ISSUED' ignored
    assert "WHITFIELD: CDL held since at least 10/04/2021" in r.message and "5.0 yrs" in r.message
    assert "MORALES: CDL held since at least 06/04/2023" in r.message and "3.4 yrs" in r.message
    assert "SANDHU: no CDL" in r.message and r.outcome == "REFER"


def test_baseline_failure_refers_never_declines(sub):
    db, s = sub
    engine.evaluate(db, s)
    r = _results(db, s)
    assert r["DRV-02"].outcome == "REFER" and "no CDL" in r["DRV-02"].message
    assert r["DRV-04"].outcome == "PASS"


def test_states_dedupe_and_config_flip(sub, monkeypatch):
    db, s = sub
    engine.evaluate(db, s)
    r = _results(db, s)
    assert r["ST-01"].outcome == "PASS"
    assert r["ST-04"].outcome == "REFER" and "2 of 5 IFTA states" in r["ST-04"].message   # KY counted once; NJ, KY outside
    assert r["ST-02"].outcome == "NOT_EVALUATED"                                              # TX miles: I-10 unknown
    cfg = engine.load_config()
    cfg["settings"]["operating_states_mode"] = "decline"
    monkeypatch.setattr(engine, "load_config", lambda: cfg)
    engine.evaluate(db, s)
    assert _results(db, s)["ST-04"].outcome == "DECLINE" and s.decision == "DECLINE"


def test_prohibited_exposures_decidable_vs_not(sub):
    db, s = sub
    engine.evaluate(db, s)
    r = _results(db, s)
    assert r["PRO-01"].outcome == "PASS" and r["PRO-07"].outcome == "PASS"
    assert r["PRO-02"].outcome == "PASS" and r["PRO-08"].outcome == "PASS"   # list complete: 50+30+20 = 100%
    assert "100%" in r["PRO-02"].message and len(r["PRO-02"].input_field_ids) == 6  # cites names and shares
    assert r["PRO-03"].outcome == "NOT_EVALUATED" and r["PRO-05"].outcome == "NOT_EVALUATED"


def test_commodity_pass_needs_a_complete_list(sub):
    # D-29: 'not listed' is evidence only when the shares add up; a partial list is not determinable
    db, s = sub
    pct = db.query(Field).filter_by(submission_id=s.id, name="percent").first()
    pct.value = "10"   # 10+30+20 = 60%
    db.commit()
    engine.evaluate(db, s)
    r = _results(db, s)
    assert r["PRO-02"].outcome == "NOT_EVALUATED" and "60" in r["PRO-02"].message


def test_listed_term_without_loss_run_is_a_conflict(sub):
    db, s = sub
    found = conflicts.detect(db, s.id)
    hits = [c for c in found if c.field_name == "loss_run"]
    assert len(hits) == 1 and hits[0].entity_key == "prior_policy:2022-03-08"
    assert "LOSS RUNS ATTACHED" in hits[0].description and "LAKESHORE" in hits[0].description


def test_tiv_is_computed_not_read_from_formula(sub):
    from app.rules import derived
    from app.rules.view import SubmissionView
    db, s = sub
    tiv = derived.computed_tiv(SubmissionView(db, s.id))
    assert tiv["cost_new_total"] == 193_000 and tiv["units_missing_stated_value"] == 4


def test_unread_pages_never_produce_a_pass():
    """A rule must not PASS on absence of evidence when pages that could hold it were unread."""
    init_db()
    db = SessionLocal()
    s = Submission(subject="unread")
    db.add(s)
    db.flush()
    d = Document(submission_id=s.id, filename="Q3 2025.pdf", sha256="u", blob_path="x", status="OK")
    db.add(d)
    db.flush()
    db.add(Page(document_id=d.id, page_no=1, doc_type="", status="MODEL_ERROR", text=""))
    db.commit()
    engine.evaluate(db, s)
    r = _results(db, s)
    assert r["ST-02"].outcome == "NOT_EVALUATED"
    assert r["SUB-01"].outcome == "NOT_EVALUATED"
    db.close()
