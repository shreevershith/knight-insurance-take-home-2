"""Score a processed submission against an expected-values file.

Checks field values, derived values, rule outcomes, decision, team and conflicts; optionally the
status of named documents and the type each named file's pages were classified as.

  .venv/Scripts/python.exe eval/run_eval.py [submission_id]
      the sample, eval/cobalt_mesa_expected.json: a REGRESSION check (D-19)
  .venv/Scripts/python.exe eval/run_eval.py --expected eval/heldout/desert_ridge_expected.json [submission_id]
      the held-out set: never used to build or tune the system, so this one MEASURES
With no submission id, the latest submission is scored (for --expected files that name a
submission_subject, the latest submission whose subject contains it).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Conflict, Document, Page, RuleResult, SessionLocal, Submission  # noqa: E402
from app.extract.normalize import name_tokens, normalize  # noqa: E402
from app.rules import derived  # noqa: E402
from app.rules.view import SubmissionView  # noqa: E402

DEFAULT = ROOT / "eval" / "cobalt_mesa_expected.json"


def check(label, ok, got, want, rows):
    rows.append((ok, label, got, want))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("submission_id", nargs="?", type=int)
    ap.add_argument("--expected", default=str(DEFAULT))
    args = ap.parse_args()
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    held_out = "heldout" in Path(args.expected).as_posix()

    with SessionLocal() as db:
        q = db.query(Submission.id)
        if args.submission_id is None and expected.get("submission_subject"):
            q = q.filter(Submission.subject.contains(expected["submission_subject"]))
        sub_id = args.submission_id or q.order_by(Submission.id.desc()).limit(1).scalar()
        sub = db.get(Submission, sub_id)
        v = SubmissionView(db, sub_id)
        rows: list = []

        for name, want in expected.get("documents", {}).items():
            doc = db.query(Document).filter_by(submission_id=sub_id, filename=name).first()
            got = doc.status if doc else "absent"
            if doc and doc.status == "OK" and doc.pages and all(p.status == want for p in doc.pages):
                got = want  # page-level failure (e.g. every page UNREADABLE) counts
            check(f"document {name}", got == want, got, want, rows)
        for name, want in expected.get("pages", {}).items():
            types = [p.doc_type for p in db.query(Page).join(Document).filter(
                Document.submission_id == sub_id, Document.filename == name)]
            check(f"classified {name}", want in types, ",".join(sorted(set(types))) or "no pages", want, rows)

        for e in expected["fields"]:
            val = v.value(e["entity"], e["field"], set(e["doc_types"]) if e.get("doc_types") else None)
            want = e["expect"]
            if want is None:
                ok = val.raw is None
            elif str(e.get("match", "")).startswith("contains:"):
                ok = bool(val.raw) and e["match"].split(":", 1)[1] in val.raw.upper()
            else:
                got_n = val.norm
                want_n = normalize(e["field"], want) if isinstance(want, str) else want
                ok = got_n == want_n or (isinstance(want, (int, float)) and not isinstance(want, bool)
                                         and isinstance(got_n, (int, float)) and abs(got_n - want) < 0.01)
            check(f"field {e['entity']}.{e['field']}", ok, val.raw, want, rows)

        d = expected.get("derived", {})
        on, _ = derived.anchor(v, "application_date")
        roster = {k: name_tokens(derived.driver_label(v, k)) for k in derived.roster(v)}
        for last, years in d.get("cdl_years", {}).items():
            # labels come as "OSEI, NATHANIEL K" or "NATHANIEL K OSEI": match on the name token
            key = next((k for k, toks in roster.items() if last.upper() in toks), None)
            ev = derived.cdl_evidence(v, key) if key else []
            got = derived.years_between(ev[0].on, on) if ev and on else None
            check(f"cdl_years {last}", got == years, got, years, rows)
        if "tiv_cost_new_total" in d:
            tiv = derived.computed_tiv(v)
            check("tiv_cost_new_total", tiv["cost_new_total"] == d["tiv_cost_new_total"], tiv["cost_new_total"],
                  d["tiv_cost_new_total"], rows)

        last = db.query(RuleResult).filter_by(submission_id=sub_id).order_by(RuleResult.id.desc()).first()
        results = {r.rule_id: r.outcome for r in db.query(RuleResult).filter_by(submission_id=sub_id, run_id=last.run_id)} if last else {}
        for rule_id, want in expected["rules"].items():
            check(f"rule {rule_id}", results.get(rule_id) == want, results.get(rule_id), want, rows)
        check("decision", sub.decision == expected["decision"], sub.decision, expected["decision"], rows)
        check("team", sub.team == expected["team"], sub.team, expected["team"], rows)

        found = db.query(Conflict).filter(Conflict.submission_id == sub_id, Conflict.status != "REPLACED").all()
        for c in expected["conflicts"]:
            hit = any(x.entity_key == c["entity"] and x.field_name == c["field"] for x in found)
            check(f"conflict {c['entity']}.{c['field']}", hit, "found" if hit else "missing", c.get("why", ""), rows)

    for ok, label, got, want in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {label:55} got={got!s:30.30} want={want}")
    passed = sum(r[0] for r in rows)
    kind = ("HELD-OUT set: only its FIRST run is a measurement; after fixes it is a regression check (RUN_LOG.md)" if held_out
            else "regression check on the sample, not an accuracy measure")
    print(f"\n{passed}/{len(rows)} checks passed (submission {sub_id}; {kind})")


if __name__ == "__main__":
    main()
