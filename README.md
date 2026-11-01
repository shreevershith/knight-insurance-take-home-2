# Knight Intake: commercial auto submission review

A working prototype for Knight Specialty's trucking program. An agent's email goes in; the system unpacks it, reads
every page (including scans and phone photos), extracts each value **with the line it came from**, finds conflicts
between documents, applies the underwriting guideline with **deterministic rules**, and shows an underwriter a
decision, the reasons, and an AI-written summary they can check.

> The AI reads documents and advises. **It never decides.** Every accept/refer/decline comes from versioned rules
> over values a person can inspect, correct or override.

## What it does

- **Intake:** an `.eml` (or loose files). Zips are unpacked, duplicates caught by hash, unsupported files flagged.
- **Classification by content, not file name.** `scan001.pdf` that is really a loss run is read as a loss run.
- **Extraction with evidence.** Each value keeps its excerpt, source page and a confidence. Code checks that the
  excerpt is really on the page and contains the value. Scans and photos are transcribed first, then read the same way.
- **Checks in code:** VIN check digits, IFTA rows must add up to the printed total, agreement across documents,
  a loss run matching the application's prior-insurance listing.
- **Conflicts** (INC vs LLC, DOB mismatches, a claim on a vehicle that isn't scheduled…) are shown with every
  source value and a suggestion, and stay open until a person decides.
- **42 guideline rules** (`app/rules/knight_v1.yaml`), each quoting its clause. A rule whose input can't be determined
  returns *not evaluated*, which refers and never passes.
- **Review UI:** status and decision, a "To review" list, rule checks, conflicts, extracted values with their evidence
  and page image, corrections with a reason (rules re-run immediately), and a full audit log.
- **AI analyst:** summary, concerns and next actions, each citing the values it relies on; citations are verified.

## Run it

Requires Python 3.12 and an Anthropic API key.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # macOS/Linux: .venv/bin/python
cp .env.example .env                                        # then set ANTHROPIC_API_KEY
.venv/Scripts/python -m uvicorn app.web.main:app --port 8000
```

Open http://localhost:8000 and drop an `.eml` (or the files) on the page.

- **Database:** SQLite under `data/` by default. For PostgreSQL, set `DATABASE_URL` in `.env`
  (`postgresql+psycopg://user:pass@localhost:5432/knight`). On Windows, `scripts/pg_setup.ps1` creates the
  database and user and checks the login (D-33).
- **Sample submission:** the case-study pack isn't in this repository. With it in `candidate_materials/`,
  `python scripts/make_sample_eml.py` builds `samples/cobalt_mesa_submission.eml`.
- **From the command line:** `python scripts/run_sample.py [path.eml]` runs the whole pipeline without the web app.

## Tests and evaluation

```bash
.venv/Scripts/python -m pytest -q                                           # 65 tests, no API calls
.venv/Scripts/python eval/run_eval.py                                       # the case-study sample
.venv/Scripts/python eval/run_eval.py --expected eval/heldout/desert_ridge_expected.json
.venv/Scripts/python eval/run_eval.py --expected eval/heldout/bluestem_expected.json
```

| Set | What it is | First run | Now |
|---|---|---|---|
| Sample (Cobalt Mesa, CA) | The case-study submission. The system was built against it, so this is a **regression check**, not an accuracy measure | n/a | **87/87** |
| Held-out #1 (Desert Ridge, AZ) | Written from scratch after the build: other carriers, a phone-photo loss run, an image-only IFTA scan, a corrupt PDF, a different roster layout | **49/72** | 72/72 |
| Held-out #2 (Bluestem, TN) | Written after the #1 fixes and run once on unchanged code: a zip with misleading names, a duplicate, a `.docx`, a PDF schedule, four IFTA returns in one PDF | **64/70** | 69/70 |

**The first-run numbers are the real measurements.** Both held-out sets were then used to fix general causes, so their
current scores are regression results. On both unseen submissions **every miss went toward "refer"**: nothing was
wrongly passed or wrongly declined. On held-out #2 the decision (DECLINE, from two independent causes) was right on the
first run. Expected values were written by hand before each first run; `scripts/make_heldout*.py` rebuilds the files
(needs `requirements-dev.txt`).

## Architecture

```
email ─► intake (zip, dedupe) ─► pages ─► classify (rules first, model if needed; scans transcribed)
      ─► extract with evidence (one schema-driven extractor; spreadsheets parsed in code)
      ─► verify (excerpt on page, validators, cross-document, IFTA totals) ─► resolve entities
      ─► conflicts ─► 42 rules (YAML + Python) ─► decision + routing ─► AI analyst (advisory)
      ─► review UI ─► corrections/resolutions (audited) ─► rules re-run
```

- **Models per task (D-31):** Haiku 5.5 classifies and extracts, Sonnet 5.5 transcribes scans and photos, Opus 5.5
  writes the analysis. A cheaper model's result that fails a code check is re-run on Opus. Measured cost: about
  **$0.62 per submission** for the 33-page sample, down from $2.37 with Opus everywhere. Every call is in a cost
  ledger (`scripts/cost_report.py`).
- **Guards that hold across all rules:** a value that couldn't be read is never reported as missing; a rule never
  passes on a low-confidence value; a decline needs cited evidence; a rule cites only the values it relies on.
- **Built vs designed.** Built: a single process with background jobs, an atomic one-job-per-submission claim and
  restart recovery, on PostgreSQL. Designed only: queue workers (SQS, `SKIP LOCKED` leases, retries), Docker, real
  mailbox intake, SSO/RBAC, FMCSA/SAFER lookups. See PLAN.md §5a and D-25.

## Where things are

| Path | What |
|---|---|
| `app/intake.py`, `app/classify.py`, `app/extract/` | Intake, page classification, extraction, evidence scoring, normalization |
| `app/resolve.py`, `app/conflicts.py` | Entity matching across documents, conflicts and duplicates |
| `app/rules/` | `knight_v1.yaml` (thresholds, keyword lists, 42 rules), the engine, guards and predicates |
| `app/pipeline.py`, `app/analyst.py`, `app/review.py` | The run, the AI analyst, human corrections |
| `app/web/` | FastAPI app and templates |
| `eval/` | Expected values, the eval runner, held-out submissions |
| `PLAN.md` | Requirements checklist, the guideline as rules, ground truth, architecture, a claims check against the code |
| `DECISIONS.md` | Every decision: what the spec said, the options, what was chosen and why (D-01 to D-35) |
| `RUN_LOG.md` | Every real model run, its cost and eval score, and what changed because of it |

## Known limitations

- Evaluated on three submissions, two of which have since been used for fixes. A new held-out submission is what would
  measure again.
- Some guideline inputs aren't in a submission at all (SAFER violations, distance to the Mexico border, requested premium,
  a 65+ driver's medical card); those rules return *not evaluated* and an underwriter checks them.
- Ambiguities in the guideline (e.g. whether "non-listed states" means the home state or every state operated in) are
  settings or documented interpretations, listed in PLAN.md §2.9 as questions for Knight.
- Single reviewer identity; no authentication.

## How it was built

Planned, decided and reviewed by me; implemented with **Claude Code** (Anthropic) working from those decisions. Every
design choice and its reason is in DECISIONS.md, and every model run in RUN_LOG.md.
