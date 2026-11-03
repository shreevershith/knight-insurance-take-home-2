"""D-57: the page shows one row per fact with every source, spreadsheet lines in words, and rule results as
"needs attention" and "fine" lists with the values used named in plain words."""
from types import SimpleNamespace as NS

from app.web.labels import based_on, readable_excerpt, rule_lines
from app.web.main import _facts


def test_rule_results_split_into_problems_and_fine_with_names_in_reading_order():
    probs, fine = rule_lines("SANDHU, DEVRAJ: no CDL (MVR class C) | OK: MORALES, LUIS E: valid CDL class A; "
                             "OSEI, NATHANIEL K: valid CDL class A")
    assert probs == ["Devraj Sandhu: no CDL (MVR class C)"]
    assert fine == ["Luis E Morales: valid CDL class A", "Nathaniel K Osei: valid CDL class A"]
    # a message that isn't per driver stays one line, even with commas and colons in it
    assert rule_lines("Miles in 4 of 8 states that aren't on the list: CO, NM, NV, OK") == (
        ["Miles in 4 of 8 states that aren't on the list: CO, NM, NV, OK"], [])


def test_spreadsheet_lines_read_as_words_and_other_quotes_are_untouched():
    assert readable_excerpt("Agency Name' | E15='BAYLINE INSURANCE AGENCY INC'") == \
        "Agency Name: BAYLINE INSURANCE AGENCY INC (cell E15)"
    assert readable_excerpt("C7='Date of Application' | E7=10/22/2026") == "Date of Application: 10/22/2026 (cells C7, E7)"
    assert readable_excerpt("Description of Operation:' | H36='TRUCKING FOR HIRE'") == \
        "Description of Operation: TRUCKING FOR HIRE (cell H36)"
    assert readable_excerpt("DOT # 9481307") == "DOT # 9481307"


def _f(i, key, name, value, status="OK", method="llm_text", conf=0.9):
    return NS(id=i, entity_key=key, name=name, value=value, status=status, method=method, confidence=conf)


def test_values_used_are_named_and_counted_in_words():
    fields = [_f(1, "applicant", "application_date", "10/22/2026")] + \
        [_f(10 + i, f"driver:D{i}", "commercial_license", "yes") for i in range(5)] + \
        [_f(20 + i, f"license_event:driver:D1:{i}", "event", "CDL") for i in range(3)] + \
        [_f(30 + i, f"license_event:driver:D1:{i}", "date", "01/01/2020") for i in range(3)]
    assert based_on(fields) == [("Application date", 1), ("Commercial license (5 drivers)", 10),
                                ("MVR history entries (3)", 20)]


def test_one_row_per_fact_with_agreeing_documents_together_and_disagreeing_ones_apart():
    items = [_f(1, "applicant", "dot_number", "9481307"), _f(2, "applicant", "dot_number", "9481307 "),
             _f(3, "applicant", "agency_phone", "(925) 555-0164"), _f(4, "applicant", "agency_phone", "(925) 555-0172"),
             _f(5, "applicant", "business_name", "COBALT MESA TRANSPORT INC", status="SUPERSEDED"),
             _f(6, "applicant", "business_name", "COBALT MESA TRANSPORT INC", method="human", conf=1.0)]
    facts = {f["name"]: f for f in _facts(items)}
    assert [[x.id for x in g] for g in facts["dot_number"]["values"]] == [[1, 2]]          # agree: one value, 2 sources
    assert [[x.id for x in g] for g in facts["agency_phone"]["values"]] == [[3], [4]]      # disagree: two values
    assert [[x.id for x in g] for g in facts["business_name"]["values"]] == [[6]]          # your decision shown
    assert [x.id for x in facts["business_name"]["replaced"]] == [5] and facts["business_name"]["human"]


def test_the_same_value_written_differently_is_one_value():
    items = [_f(1, "driver:D1", "full_name", "HECTOR RAMOS"), _f(2, "driver:D1", "full_name", "RAMOS, HECTOR"),
             _f(3, "applicant", "effective_date", "12/15/2026."), _f(4, "applicant", "effective_date", "12/15/2026")]
    facts = {f["name"]: f for f in _facts(items)}
    assert [[x.id for x in g] for g in facts["full_name"]["values"]] == [[1, 2]]
    assert [[x.id for x in g] for g in facts["effective_date"]["values"]] == [[3, 4]]
