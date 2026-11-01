"""Score the latest processed submission against eval/cobalt_mesa_expected.json.

Regression check on the sample (D-19): field values, derived facts, rule outcomes, decision, conflicts.
Usage: .venv/Scripts/python.exe eval/run_eval.py [submission_id]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Conflict, RuleResult, SessionLocal, Submission  # noqa: E402
from app.extract.normalize import name_tokens, normalize  # noqa: E402
from app.rules import derived  # noqa: E402
from app.rules.view import SubmissionView  # noqa: E402

EXPECTED = json.loads((ROOT / "eval" / "cobalt_mesa_expected.json").read_text(encoding="utf-8"))


def check(label, ok, got, want, rows):
    rows.append((ok, label, got, want))


def main() -> None:
    with SessionLocal() as db:
        sub_id = int(sys.argv[1]) if len(sys.argv) > 1 else db.query(Submission.id).order_by(Submission.id.desc()).limit(1).scalar()
        sub = db.get(Submission, sub_id)
        v = SubmissionView(db, sub_id)
        rows: list = []

        for e in EXPECTED["fields"]:
            val = v.value(e["entity"], e["field"], set(e["doc_types"]) if e.get("doc_types") else None)
            want = e["expect"]
            if want is None:
                ok = val.raw is None
            elif str(e.get("match", "")).startswith("contains:"):
                ok = bool(val.raw) and e["match"].split(":", 1)[1] in val.raw.upper()
            else:
                got_n = val.norm
                want_n = normalize(e["field"], want) if isinstance(want, str) else want
                ok = got_n == want_n or (isinstance(want, (int, float)) and got_n is not None and abs(got_n - want) < 0.01)
            check(f"field {e['entity']}.{e['field']}", ok, val.raw, want, rows)

        d = EXPECTED["derived"]
        on, _ = derived.anchor(v, "application_date")
        roster = {k: name_tokens(derived.driver_label(v, k)) for k in derived.roster(v)}
        for last, years in d["cdl_years"].items():
            # labels come as "OSEI, NATHANIEL K" or "NATHANIEL K OSEI": match on the name token
            key = next((k for k, toks in roster.items() if last.upper() in toks), None)
            ev = derived.cdl_evidence(v, key) if key else []
            got = derived.years_between(ev[0].on, on) if ev and on else None
            check(f"cdl_years {last}", got == years, got, years, rows)
        tiv = derived.computed_tiv(v)
        check("tiv_cost_new_total", tiv["cost_new_total"] == d["tiv_cost_new_total"], tiv["cost_new_total"], d["tiv_cost_new_total"], rows)

        last = db.query(RuleResult).filter_by(submission_id=sub_id).order_by(RuleResult.id.desc()).first()
        results = {r.rule_id: r.outcome for r in db.query(RuleResult).filter_by(submission_id=sub_id, run_id=last.run_id)} if last else {}
        for rule_id, want in EXPECTED["rules"].items():
            check(f"rule {rule_id}", results.get(rule_id) == want, results.get(rule_id), want, rows)
        check("decision", sub.decision == EXPECTED["decision"], sub.decision, EXPECTED["decision"], rows)
        check("team", sub.team == EXPECTED["team"], sub.team, EXPECTED["team"], rows)

        found = db.query(Conflict).filter_by(submission_id=sub_id).all()
        for c in EXPECTED["conflicts"]:
            hit = any(x.entity_key == c["entity"] and x.field_name == c["field"] for x in found)
            check(f"conflict {c['entity']}.{c['field']}", hit, "found" if hit else "missing", c.get("why", ""), rows)

    for ok, label, got, want in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {label:55} got={got!s:30.30} want={want}")
    passed = sum(r[0] for r in rows)
    print(f"\n{passed}/{len(rows)} checks passed (submission {sub_id}; regression check on the sample, not an accuracy measure)")


if __name__ == "__main__":
    main()
