# Code explanation

A guided tour of the code: what every file does, one submission traced from upload to decision, and a summary of
the core logic with honest notes on what to refactor next. For the big picture and diagrams, read
[ARCHITECTURE.md](ARCHITECTURE.md) first.

- [1. File-by-file inventory](#1-file-by-file-inventory)
- [2. End-to-end execution trace](#2-end-to-end-execution-trace)
- [3. Developer self-review](#3-developer-self-review)

---

## 1. File-by-file inventory

### Platform (`app/`)

| File | Responsibility | Key names |
|---|---|---|
| `app/config.py` | Every setting, read from `.env` / environment. Paths (`DATA_DIR`, blobs, page images, LLM cache, cost ledger), database URL (Postgres or SQLite fallback), model routing per task, prices, job and concurrency settings. | `route(tier)`, `TIER_DEFAULTS`, `cost_usd`, `stored_path` / `data_path`, `JOB_MODE`, `LLM_CONCURRENCY` |
| `app/db.py` | SQLAlchemy models and the audit helper. Tables: `submission`, `document`, `page`, `field`, `conflict`, `rule_result`, `audit_event`, `job_log`, `job`. `init_db()` creates missing tables. | `Submission`, `Document`, `Page`, `Field`, `Conflict`, `RuleResult`, `Job`, `audit()` |
| `app/llm.py` | The only place that calls Claude. One request → one JSON object matching a schema (structured output). Picks the model from the task's tier, or the escalation model. Caches each response on disk by a hash of the exact request; writes one cost-ledger line per call (cache hits too). Turns API failures, refusals and truncation into `LLMError`. | `structured_call`, `image_block`, `tier_for`, `run_id` (a `ContextVar`), `LLMError` |
| `app/log.py` | JSON logs with context (submission, page). Masks SSNs, FEINs, license numbers and full dates before anything is written. | `setup_logging`, `get_logger`, `mask_pii` |

### Intake and pages

| File | Responsibility | Key names |
|---|---|---|
| `app/intake.py` | Turns an email or uploaded files into stored documents, then pages. Stores each file by SHA-256 (duplicates marked `DUPLICATE`), unpacks zips (depth and member limits), reads the email body into `email-body.txt`, and opens emails found inside the submission (forwarded as attachments, zipped, or uploaded with other files). Splits files into pages: PDF pages (text layer + 150 dpi image; image-only if under 40 characters of text), XLSX sheets (cell dump such as `B4='DEVRAJ'`, with formula notes), Word body and tables, text, or one image (turned upright from EXIF, resized to 2000 px when needed). Anything else: `UNSUPPORTED` with the reason; a corrupt file: `UNREADABLE`. | `ingest_eml`, `ingest_files`, `email_body`, `split_pages`, `docx_text`, `_page_image` |
| `app/classify.py` | Decides each page's document type by content. Text pages: regex content signatures first (free, explainable); if none is decisive, a Haiku call. Image pages: one vision call that classifies and transcribes the page (Sonnet), re-run on Opus if it fails or says "unreadable". | `classify_text_rules`, `classify_page`, `DOC_TYPES` |

### Extraction (`app/extract/`)

| File | Responsibility | Key names |
|---|---|---|
| `spec.py` | What to extract from each document type: groups (single or list), entity types, and field names with descriptions. The JSON schema for extraction is generated from it, so adding a field is one line. | `SPEC`, `COMPARABLE` |
| `llm_extract.py` | Builds the schema and prompt for one page's type and calls the model. Returns facts `{group, entity_type, index, field, value, excerpt}`; the excerpt must be the line the value was read from. | `extract_page`, `build_schema` |
| `xlsx_extract.py` | Reads driver and vehicle spreadsheets in code: finds the header row by meaning (`"DOB"`, `"Date of Birth"`, `"License #"` …) and turns rows into facts. Returns nothing for a sheet it can't map, so the model reads it instead. | `extract_sheet`, `DRIVER_HEADERS`, `VEHICLE_HEADERS` |
| `table_transcript.py` | Reads IFTA jurisdiction tables from a scan's transcript in code: maps columns by header synonyms, finds the total row and the quarter. Gives up (returns `None`) rather than guessing. | `parse_ifta_page` |
| `evidence.py` | Confidence for one value: method prior (spreadsheet 0.98, text 0.90, scan 0.75) × "is the excerpt really on the page" × "is the value in the excerpt" × validators. Below 0.85 the value is `LOW_CONFIDENCE`. | `score`, `excerpt_in_source`, `REVIEW_THRESHOLD` |
| `normalize.py` | Turns values into comparable forms without overwriting them: dates to ISO, money and counts to numbers, yes/no to booleans, names, states, IDs; VIN check digits; line of business to AL / MTC / APD. | `normalize`, `validate`, `parse_date`, `vin_check`, `coverage_line`, `names_compatible` |

### Pipeline and cross-checks

| File | Responsibility | Key names |
|---|---|---|
| `app/pipeline.py` | Orchestrates one run: reset (nothing deleted, old values marked `REPLACED`), split, classify (parallel), inherit continuation pages, extract (model calls parallel, storage in page order), re-apply human decisions, cross-checks, IFTA reconciliation and re-read, conflicts, rules, analyst. Also the job claim, restart recovery and the analysis-only re-run. | `run`, `claim`, `reset_interrupted`, `run_analysis`, `_parallel`, `_page_facts`, `_extract`, `_llm_extract`, `_ifta_reconciliation`, `_escalate_ifta` |
| `app/resolve.py` | Gives every fact a stable entity key so the same driver, vehicle or policy lines up across documents: drivers by license number (else name + DOB), vehicles by VIN, loss runs by line + policy number, IFTA by quarter. Child items (violations, claims, IFTA rows) take their parent's key. | `assign_keys` |
| `app/conflicts.py` | Finds disagreements: same entity + field with different normalized values across documents; claims on vehicles or drivers that aren't scheduled; prior terms listed without a loss run; a stale duplicate MVR (superseded, with the reason). Each conflict lists every source value and suggests one (`_suggest`): the value most independent documents agree on; on a tie, the more authoritative document (`SOURCE_PRIORITY`: MVR > application > license photo, loss run, IFTA > driver/vehicle lists > quote form); for a vehicle's make, the VIN's manufacturer prefix. Claim and missing-loss-run conflicts carry a plain sentence and a next step. | `detect`, `active_fields`, `SOURCE_PRIORITY` |
| `app/analyst.py` | The AI summary. Builds a compact context (values with ids, conflicts, rule results, IFTA aggregates computed in code, the agent's email as context) and asks Opus for a summary, concerns and next actions, each with cited ids and a confidence. Citations to ids that don't exist are removed in code. | `analyze`, `build_context`, `agent_email` |
| `app/review.py` | Human actions: override a value, resolve a conflict by choosing a value, or acknowledge one with nothing to choose (a reason or note is required). Each creates a `method="human"` field, marks the extracted value `SUPERSEDED`, writes an audit event and re-runs the rules. Refused while a job is running. | `override_field`, `resolve_conflict`, `rerun_rules`, `ReviewError` |

### Rules (`app/rules/`)

| File | Responsibility | Key names |
|---|---|---|
| `knight_v1.yaml` | The guideline as config: version, settings, numeric params (states, ages, day limits, points), keyword patterns (DUI, tractor, prohibited commodities …) and 42 rules, each with its title, the clause it quotes, its failing outcome, and (for three) `undetermined: VERIFY`. | — |
| `engine.py` | Loads and validates the YAML (a broken file keeps the last good one), runs every rule, applies the central guards, stores results, and computes the decision and team. Also holds the predicates for completeness, drivers, states, prohibited exposures and routing. | `evaluate`, `load_config`, `validate_config`, `_apply_guards`, `Result`, `missing`, `@rule` |
| `guideline_rules.py` | The remaining predicates: appetite and coverage, new ventures, Illinois, loss runs and IFTA windows, MVR age, licenses, points, DUI and unacceptable history, selective exposures, prior insurance. | `@rule("APP-01")` … `@rule("PRI-03")` |
| `view.py` | What rules read: for each fact, **one** value (a human decision first, then verified over low-confidence, then the most authoritative source), with the field ids it came from. Also lists pages that couldn't be read. | `SubmissionView.value`, `entities`, `unread` |
| `derived.py` | Facts computed from several values: driver roster, CDL evidence and years, domicile state (also from a one-line address), operating states from IFTA, total insured value, the anchor date. | `cdl_evidence`, `domicile_state`, `operating_states`, `computed_tiv` |

### Web (`app/web/`)

| File | Responsibility |
|---|---|
| `main.py` | FastAPI routes. `GET /` (upload + list), `POST /submissions` (ingest + start), `GET /submissions/{id}` (the review page), `GET /submissions/{id}/status` (polled while running), `POST …/reprocess`, `POST …/rerun-analysis`, `POST …/rerun-rules`, `POST /fields/{id}/override`, `POST /conflicts/{id}/resolve`, `GET /fields/{id}` (evidence drawer data), `GET /pages/{id}/image`, `GET /ops` (operations). Starts jobs in a thread or enqueues them (`JOB_MODE`). |
| `labels.py` | Plain-language labels used across the page: field names (`field_label`), document types, how a value was read (`method_label`), check results as sentences (`check_lines`), audit actions, statuses and file errors (`plain_error`). Also readable names for entity keys: a `driver:<license>` key → "Maria Lopez", `ifta:2026-03-31` → "Q1 2026", a vehicle → "2022 Volvo · VIN …302214". |
| `templates/base.html` | Layout, header, design tokens and shared styles, the evidence drawer and toasts. |
| `templates/index.html` | Upload panel and recent submissions. |
| `templates/submission.html` | Status and decision; To review grouped for an underwriter (reasons to refer, documents that disagree with each value's source, verify before quoting, couldn't be checked); AI summary with readable citation chips; rule checks with a legend for the codes; conflicts (choose a value, or acknowledge); extracted values with evidence; documents; audit log; live progress while a job runs. |
| `templates/ops.html` | Needs attention, job queue, cost per run. |

### Jobs

| File | Responsibility |
|---|---|
| `app/jobs.py` | The queue on Postgres: `enqueue`, `claim_next` (`FOR UPDATE SKIP LOCKED`, lease, takeover of an expired lease), `renew`, `finish` (DONE, retry with backoff, or FAILED). |
| `app/worker.py` | `python -m app.worker`: claims one job at a time, runs it with a heartbeat that renews the lease, records the outcome; stops cleanly on SIGTERM. |

### Evaluation, scripts, tests, deployment

| Path | Responsibility |
|---|---|
| `eval/run_eval.py` | Scores a processed submission against an answer key: document status, page types, field values, derived values, every rule outcome, decision, team, expected conflicts. |
| `eval/cobalt_mesa_expected.json` | Answer key for the case-study sample (87 checks; a regression check, since the system was built against it). |
| `eval/heldout/*_expected.json`, `*.eml`, folders | Four submissions written after the build, answer keys written before their first run: Desert Ridge, Bluestem, Holston Valley (clean fleet), Red River (forwarded email). |
| `eval/experiments/*.sh` | The model-mix cost experiments (RUN_LOG). |
| `scripts/run_sample.py` | Runs the whole pipeline from the command line on an `.eml`, no web server. |
| `scripts/cost_report.py` | Cost per run (and per task for one run) from the ledger. |
| `scripts/make_sample_eml.py`, `make_heldout*.py` | Build the sample email from the case-study files; generate the four held-out submissions. |
| `scripts/pg_setup.ps1` | Creates the app's Postgres user and database from `DATABASE_URL` (Windows). |
| `tests/` | 87 tests, no API calls: guards, rules per section, reprocess keeping human work, cost controls and routing, queue and leases, Word and email intake, photos, review fixes. `conftest.py` points every test at a throwaway data folder and database. |
| `Dockerfile`, `docker-compose.yml`, `.dockerignore` | Image (Python 3.12, non-root, health check) and the stack: app, worker(s), Postgres 18. |
| `PLAN.md`, `DECISIONS.md`, `RUN_LOG.md` | Requirements and the guideline as rules; every decision with options and reasons; every real model run with its cost and score. |

---

## 2. End-to-end execution trace

One email, from upload to the underwriter's corrections. The stack runs with `docker compose up` (queue mode).

**Step 1. The agent's email is uploaded.**
The browser posts the `.eml` to `POST /submissions` (`app/web/main.py: create_submission`). Because it's a single
`.eml`, it goes to `intake.ingest_eml`.

**Step 2. Intake stores everything.**
`ingest_eml` creates a `Submission` (sender, subject, email hash) and an audit event. It reads the body with
`email_body` (plain text, or HTML converted to text) and stores it as `email-body.txt`. Each attachment goes through
`_add_file`: stored under `data/blobs/` by SHA-256; a hash already seen becomes `DUPLICATE`; a zip is unpacked
recursively (`_unpack_zip`), each member becoming its own `Document` with a parent link.

**Step 3. A job is queued.**
`main._start` calls `pipeline.claim` (an atomic `UPDATE … WHERE status <> 'PROCESSING'`, so only one job per
submission) and then `jobs.enqueue`. The browser is redirected to `/submissions/{id}`, which polls
`/submissions/{id}/status` every second for the stage and new log lines.

**Step 4. A worker takes the job.**
`worker.run_one` calls `jobs.claim_next`, which selects the oldest ready job with `FOR UPDATE SKIP LOCKED`, sets it
RUNNING with a 120-second lease, and returns it. A heartbeat thread renews the lease every 40 seconds while
`pipeline.run(id, claimed=True)` runs.

**Step 5. Reset without deleting.**
`pipeline.reset_for_reprocess` marks values and conflicts from any earlier run `REPLACED` (kept for audit), clears
the decision and summary, and resets page statuses. Human decisions stay active.

**Step 6. Split into pages.**
`intake.split_pages` for each document: PDF pages get their text layer and a rendered image; spreadsheets get a cell
dump per sheet; Word and text files their text; images become one page (turned upright, resized if needed).

**Step 7. Classify every page, in parallel.**
`pipeline._classify_all` copies each page into a plain snapshot and runs `classify.classify_page` six at a time.
Text pages try `classify_text_rules` first (for example "driver record" plus "violations/convictions" → MVR; "jurisdiction summary" plus "taxable miles" → IFTA_RETURN); otherwise Haiku decides.
Image pages get one Sonnet vision call that returns the type **and** a transcript, which becomes the page's text.
Back on the job's thread, results are written to the pages; a page with an unknown or low-confidence type is marked
`UNCLASSIFIED`. Then `_inherit_continuations` gives a continuation page its document's type (an IFTA table's second
page).

**Step 8. Extract values.**
`pipeline._prepare_facts` runs `_page_facts` for every extractable page, six at a time:
- a driver or vehicle spreadsheet → `xlsx_extract.extract_sheet` (code);
- otherwise → `_llm_extract` → `llm_extract.extract_page` (Haiku), which re-runs on Opus if the result fails a check
  (no values, a missing key field, or more than 20% of excerpts not on the page).

Scanned IFTA pages wait and run in page order, because `table_transcript.parse_ifta_page` carries the table header
from one page to the next. Then, in page order, `_extract`:
1. `resolve.assign_keys` gives each fact its entity key (`driver:112204871`, `vehicle:1XKYD…`, `loss_run:AL:SRC5520317`);
2. `normalize.validate` runs validators (VIN check digit, dates);
3. `evidence.score` computes the confidence;
4. a `Field` row is stored with value, excerpt, page, method, confidence and status (`OK` or `LOW_CONFIDENCE`).

**Step 9. Re-apply human decisions.**
`reapply_human_decisions` marks fresh values `SUPERSEDED` where a person already decided the same
`(entity_key, field)`.

**Step 10. Cross-checks.**
- `_cross_document_confirmation`: the same value in two independent documents raises confidence to at least 0.9.
- `_loss_runs_confirmed_by_listing`: a loss run's policy number and term matching the application's prior-insurance
  listing is confirmed.
- `_ifta_reconciliation`: each IFTA return's jurisdiction rows must add up to its printed total. If a scanned return
  doesn't, `_escalate_ifta` re-reads that document on Opus and checks again; still failing, its values stay
  `LOW_CONFIDENCE`.

**Step 11. Conflicts.**
`conflicts.detect` compares normalized values per `(entity, field)` across documents (names with INC vs LLC, DOBs,
VIN makes, unit counts, phone numbers …), checks claims against the schedules, and finds listed terms with no loss run.
Each `Conflict` is OPEN with every source value and a suggestion.

**Step 12. Rules.**
`engine.evaluate` loads `knight_v1.yaml`, builds a `SubmissionView` (one value per fact) and runs the 42 predicates.
Each returns `Result(passed, message, field_ids)`:
- `passed=True` → PASS; `False` → the rule's outcome (REFER, DECLINE, FLAG); `None` → NOT_EVALUATED, or VERIFY for
  the three rules marked `undetermined: VERIFY`;
- `_apply_guards`: a DECLINE with no cited evidence becomes NOT_EVALUATED; a PASS that relies on a `LOW_CONFIDENCE`
  value becomes NOT_EVALUATED; such a DECLINE becomes REFER;
- the worst outcome sets the decision: DECLINE, REFER, QUOTE_AFTER_CHECKS (only VERIFY items open) or
  READY_TO_QUOTE. RTE-01 sets the team (1–10 units Small Fleet, 11+ Fleet).

Every result is stored with its message, the field ids it used and the rules version (YAML version + file hash).

**Step 13. AI analyst.**
`analyst.analyze` sends Opus the values (with ids), conflicts, rule results, IFTA aggregates and the agent's email.
It returns a summary, concerns and next actions with cited ids and confidence; invalid citations are removed. If the
call fails, the summary records the error and the decision is unaffected.

**Step 14. Done.**
The submission becomes DONE; `jobs.finish` marks the job DONE (or re-queues it with a delay, or marks it FAILED).
The page reloads to show the decision, the "To review" list, the AI summary, the rules, conflicts and every value
with its source.

**Step 15. The underwriter reviews.**
- Clicking a value opens the evidence drawer (`GET /fields/{id}`): the quoted line, checks, and the page image.
- Correcting a value (`POST /fields/{id}/override`) or picking a conflict's value (`POST /conflicts/{id}/resolve`)
  goes through `review.py`: a human `Field`, the old value `SUPERSEDED`, an audit event, and the rules re-run at once.
  A conflict with nothing to choose (a claim on an unscheduled driver) is acknowledged with a note instead.
  The decision may change; the AI summary is marked out of date until regenerated.

---

## 3. Developer self-review

### What I built, in one paragraph

A pipeline that reads a messy insurance submission like a careful assistant: every page classified by content,
every value tied to the line it came from with a confidence computed in code, independent documents cross-checked,
conflicts surfaced instead of silently merged, and the underwriting guideline applied by deterministic, versioned
rules that refuse to pass anything they couldn't verify. A language model does the reading and writes an advisory
summary; it never makes the decision. Everything a person changes is recorded and survives re-processing.

### Core patterns and algorithms

| Pattern | Where | Why it matters |
|---|---|---|
| **Evidence-checked extraction** | `extract/evidence.py` | The model must quote its source line; code checks the quote is on the page and contains the value. A hallucinated value loses most of its confidence. |
| **Code first, model second** | `classify_text_rules`, `xlsx_extract`, `table_transcript` | Free, explainable, and the model only handles what code can't. |
| **Cheap model + verification + escalation** | `pipeline._llm_extract`, `classify.classify_page`, `_escalate_ifta` | A cheaper model can cost an extra call, never a silently wrong value. |
| **Entity resolution by stable keys** | `resolve.assign_keys` | Lets values from different documents meet, which conflicts and cross-checks need. |
| **Reconciliation as a confidence signal** | `_ifta_reconciliation` | If the transcribed rows add up to the printed total, the transcription is very likely right. |
| **Tri-state rule results + central guards** | `engine.Result`, `_apply_guards` | Unknown is a first-class answer; no rule can pass on a guess. |
| **Config-driven rules** | `knight_v1.yaml`, `@rule` registry | Thresholds and keyword lists change without code; the rules version is stored with each result. |
| **Append-only data with human overrides** | `Field.status`, `review.py`, `reapply_human_decisions` | Full audit trail; reprocessing never loses a person's work. |
| **Database job queue** | `jobs.py` (`SKIP LOCKED`, leases) | Safe concurrency and takeover with no extra infrastructure. |
| **Request-hash cache + cost ledger** | `llm.structured_call` | Repeatable runs, measurable cost per run and per task. |

### Where I would refactor next

1. **Database migrations.** Tables are created with `create_all`, which adds new tables but never alters existing
   ones. Adding a column to a live database needs a migration tool (Alembic).
2. **`pipeline.py` is long (≈510 lines).** It mixes orchestration, extraction, cross-checks and IFTA escalation. Each
   stage could be its own module with a common interface, which would also make stages easier to test alone.
3. **Rules are split across `engine.py` and `guideline_rules.py`** by when they were written, not by guideline
   section. Grouping predicates by section (one module each) would make the guideline easier to follow in code.
4. **`submission.html` is ≈590 lines** with its styles inline. Splitting it into partials (status, rules, conflicts,
   values) and moving shared styles into a stylesheet would make UI changes safer.
5. **Confidence priors are hand-set** (0.98 / 0.90 / 0.75, threshold 0.85). They should be calibrated from real
   reviewer overrides, per method and field.
6. **One known extraction gap**: a state written only inside a one-line address is not stored as a `mailing_state`
   field (the rules derive it in code, so decisions are right).
7. **Outside data**: SAFER lookups and route data would turn the three VERIFY checks into automatic answers.
8. **Access control**: there is a single reviewer identity; SSO and roles are designed, not built.
9. **Evaluation size**: four submissions. More held-out submissions, ideally real redacted ones, are what would
   measure accuracy with confidence.
