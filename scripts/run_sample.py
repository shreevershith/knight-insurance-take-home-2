"""Ingest the sample .eml and run the full pipeline synchronously (CLI check, no web server)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pipeline  # noqa: E402
from app.db import Conflict, Page, RuleResult, SessionLocal, Submission, init_db  # noqa: E402
from app.intake import ingest_eml  # noqa: E402
from app.log import setup_logging  # noqa: E402

EML = Path(__file__).resolve().parent.parent / "samples" / "cobalt_mesa_submission.eml"


def main() -> None:
    setup_logging("WARNING")
    init_db()
    with SessionLocal() as db:
        sub = ingest_eml(db, EML.read_bytes())
        db.commit()
        sub_id = sub.id
    pipeline.run(sub_id)
    with SessionLocal() as db:
        sub = db.get(Submission, sub_id)
        print(f"\nsubmission {sub_id}: {sub.status} decision={sub.decision} team={sub.team}")
        statuses = {}
        for p in db.query(Page).join(Page.document).filter_by(submission_id=sub_id):
            statuses[p.status] = statuses.get(p.status, 0) + 1
        print("pages:", statuses)
        last = db.query(RuleResult).filter_by(submission_id=sub_id).order_by(RuleResult.id.desc()).first()
        for r in db.query(RuleResult).filter_by(submission_id=sub_id, run_id=last.run_id):
            print(f"  {r.rule_id:10} {r.outcome:14} {r.message[:150]}")
        for c in db.query(Conflict).filter_by(submission_id=sub_id):
            print(f"  [{c.kind}] {c.entity_key}.{c.field_name}: " + " | ".join(f"{v['value']} ({v['source'][:30]})" for v in c.values))
        if sub.summary.get("error"):
            print("analyst:", sub.summary["error"])


if __name__ == "__main__":
    main()
