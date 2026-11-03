# Knight Intake

**An AI-assisted intake system that turns an insurance agent's commercial-auto email into a checked, evidence-backed
underwriting decision.**

Knight Specialty's trucking program receives submissions by email: applications, driver and vehicle lists, motor
vehicle records (MVRs), loss runs, fuel-tax (IFTA) returns, scans and phone photos, often misnamed, duplicated or
inconsistent. Knight Intake reads every page, extracts each value **with the line it came from**, finds conflicts
between documents, applies the underwriting guideline as **deterministic rules**, and shows an underwriter the
decision, the reasons, and an AI-written summary they can verify.

> The AI reads documents and advises. **It never decides.** Every decision comes from versioned rules over values a
> person can inspect, correct or override.

**Documentation:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagrams, data flow, design decisions) ·
[docs/CODE_EXPLANATION.md](docs/CODE_EXPLANATION.md) (every file, a full execution trace, self-review) ·
[DECISIONS.md](DECISIONS.md) · [PLAN.md](PLAN.md) · [RUN_LOG.md](RUN_LOG.md)

---

## Features

- **Email intake.** An `.eml` file, or files uploaded directly with an optional sender and note: the email body
  itself, emails forwarded as attachments or inside zips, PDF, Excel (`.xlsx`), Word (`.docx`), text, images and zips. Zips are unpacked, duplicates caught by content hash, phone photos turned upright and sized
  for the model. Anything else (TIFF, HEIC, `.doc`, `.msg`) is flagged UNSUPPORTED with the reason, never dropped.
- **Classification by content, not file name.** A file called `scan001.pdf` that is really a loss run is read as a
  loss run; a vehicle list saved as `Book1.xlsx` is read as a vehicle schedule.
- **Extraction with evidence.** Every value keeps its quoted source line, page and a confidence computed in code
  (is the quote really on the page, does it contain the value, do validators pass). Scans, photos and handwriting are
  transcribed first, then read the same way.
- **Checks in code.** VIN check digits, IFTA rows adding up to the printed total, agreement across documents, loss
  runs matching the application's prior-insurance listing.
- **Conflicts surfaced, never silently resolved.** INC vs LLC, DOB mismatches, a claim on an unscheduled vehicle:
  each shown with every source value and the document it came from, and a suggestion (the value most documents
  agree on; on a tie, the more authoritative document; for a vehicle's make, the VIN). Open until a person decides;
  where there's nothing to choose (a claim on an unscheduled driver), the person acknowledges it with a note.
- **42 guideline rules** (`app/rules/knight_v1.yaml`), each quoting its clause. Unknown inputs refer and never pass;
  the three checks that need outside data (SAFER, border distance, the quoted premium) are listed as "check before
  quoting".
- **Decisions:** DECLINE · REFER · **Ready to quote, after checks** · Ready to quote, and routing to Small Fleet
  (1–10 power units) or Fleet (11+).
- **AI analyst.** A summary, underwriting concerns and next actions, each citing the values it relies on (citations
  verified in code), with the agent's email as context.
- **Draft reply to the agent.** The email the underwriter would send next, asking for exactly what's missing or in
  question (or a plain declination). Code chooses what it covers; the model only words it; editable, copied by the
  underwriter, never sent by the app.
- **Commodities read for meaning, not only keywords.** The model sorts each commodity against the prohibited cargo
  (lithium batteries, towing, waste) with a reason; the rules compare that with the keyword, so "paper towels" isn't
  towing and "Li-ion cells" are batteries. An unsure or disputed result asks a person; it never declines on its own.
- **Review UI.** Decision and reasons, a "To review" list, rule checks with what changed since the last run,
  conflicts, every value with its evidence and page image, corrections with a required reason (rules re-run at once),
  and a full audit log.
- **Operations page (`/ops`).** Failed or interrupted submissions, unreadable files, the job queue, and cost per run.
- **LangGraph and LangChain.** The run is a LangGraph graph: each stage a node, a conditional edge for the IFTA
  re-read, checkpoints in Postgres, and human review as `interrupt()`/resume, so every correction resumes the paused
  run. Model requests go through LangChain's `ChatAnthropic`, with our own cache and cost ledger around it. Both are
  switchable back to plain code, and were shown to give identical results.
- **Langfuse tracing.** Every run, review action and regenerate is a trace (one session per submission): graph
  nodes as spans, every model call as a generation with model, tokens and cost, and scores from our own checks
  (quoted lines verified, escalations, cost per run, corrections by field). Document text and personal data are
  not sent by default. `scripts/langfuse_dashboard.py` builds a dashboard around three questions: cost, trust, review.
- **Reliable processing.** A job queue on Postgres with workers (`SKIP LOCKED`, leases, retries, takeover when a
  worker dies); page calls run in parallel inside a job.
- **Cost control.** A model per task (Haiku, Sonnet, Opus), escalation only when a code check fails, a response cache
  (switch it off with `LLM_CACHE=off` for live runs) and a cost ledger: about **$0.55–0.66 per submission** for the
  33-page sample; fully live, 136 s.

## Prerequisites

| Need | For |
|---|---|
| **Python 3.12** | Running the app locally and the tests |
| **An Anthropic API key** | Classification of unclear pages, scans, extraction and the AI analyst |
| **Docker Desktop** (optional) | Running the full stack (app, workers, PostgreSQL 18) with one command |
| **PostgreSQL 18** (optional, local) | Only if you run locally against Postgres instead of the default SQLite |

## Installation and setup

### Option A: Docker (recommended: app + workers + Postgres)

1. Copy the example settings and fill in two values:

   ```bash
   cp .env.example .env
   ```

   In `.env`, set `ANTHROPIC_API_KEY=` and `POSTGRES_PASSWORD=` (any long random string; it is only used inside the
   Docker network).

2. Build and start:

   ```bash
   docker compose up --build -d
   ```

3. Open http://localhost:8000.

To handle more submissions at once, add workers:

```bash
docker compose up -d --scale worker=3
```

### Option B: Local Python

1. Create a virtual environment and install the dependencies:

   ```bash
   python -m venv .venv
   .venv/Scripts/python -m pip install -r requirements.txt
   ```

   (On macOS or Linux use `.venv/bin/python`.)

2. Copy the settings and set your API key:

   ```bash
   cp .env.example .env
   ```

3. Start the web app:

   ```bash
   .venv/Scripts/python -m uvicorn app.web.main:app --port 8000
   ```

4. Open http://localhost:8000.

Locally the database is SQLite under `data/` and jobs run inside the web process (`JOB_MODE=thread`), so no worker
is needed. To use a local PostgreSQL instead, set `DATABASE_URL` in `.env`; on Windows,
`scripts/pg_setup.ps1` creates the database and user from that URL.

## Usage

**Process a submission in the browser.** Drop an agent's `.eml` (or the files) on the upload panel and choose
*Process submission*. The page shows each stage live; when it finishes you see the decision, the items to review,
the AI summary, every rule, conflicts and every extracted value with its source.

**Review.** Click a value to see the quoted line and the page it came from. Correct a value or pick the right side
of a conflict, with a reason: the rules re-run immediately and the audit log records it.

**Run the pipeline from the command line** (no web server):

```bash
.venv/Scripts/python scripts/run_sample.py eval/heldout/holston.eml
```

**Score a processed submission against its answer key:**

```bash
.venv/Scripts/python eval/run_eval.py --expected eval/heldout/holston_expected.json
```

**See what runs cost:**

```bash
.venv/Scripts/python scripts/cost_report.py
```

**Run the tests** (no API calls):

```bash
.venv/Scripts/python -m pytest -q
```

Inside Docker, prefix commands with `docker compose exec app`, for example
`docker compose exec app python -m pytest -q`.

The case-study sample package is not in this repository. With its files in `candidate_materials/`,
`python scripts/make_sample_eml.py` builds `samples/cobalt_mesa_submission.eml`. The three held-out submissions are
included under `eval/heldout/` (`scripts/make_heldout*.py` rebuilds them; needs `requirements-dev.txt`).

## Reading the results page

Each submission page answers, top to bottom: **what's the decision, what needs a person, and why.**

- **Decision.** *Decline* · *Refer to underwriter* · *Ready to quote, after checks* (only outside-data checks left) ·
  *Ready to quote*, with the team: Small Fleet (1–10 power units) or Fleet (11+).
- **To review.** Everything that needs a person, grouped: reasons to refer or decline, documents that disagree (each
  value with the document it came from), what to verify before quoting, and what couldn't be checked.
- **Extracted data.** One row per fact: the value once, with every document and line it was read from (spreadsheet
  cells in words, e.g. "Agency Name: BAYLINE INSURANCE AGENCY INC (cell E15)"). When documents disagree, each value is
  shown with its sources and *Sources disagree →* jumps to the disagreement; readings replaced by your decision fold
  under "earlier readings".
- **Rule checks.** What needs attention as a list, the drivers or items that are fine folded under "N fine", the
  guideline sentence, and the values used in words ("Commercial license (5 drivers)", "MVR history entries (16)").
- **Underwriter summary.** Written by the AI: concerns and next actions, each citing the values it relies on. Advisory
  only; it never changes the decision. Below it, **Draft reply to the agent**: edit, Copy, send it from your own email.
- **Rule checks.** All 42 rules with their outcome, the guideline sentence they implement and the values they used.
  The codes are for reference; the prefix is the guideline section:

  | Code | Guideline section |
  |---|---|
  | APP, COV | Program appetite and coverage |
  | NV | New ventures |
  | ST | Available states |
  | SUB | Submission requirements |
  | DRV | Drivers: license, CDL, experience, age, points, unacceptable history |
  | PRO, SEL | Prohibited exposures (decline) and selective exposures (refer) |
  | PRI | Prior insurance |
  | RTE, CONFLICTS | Routing and conflicting information |

  The rules are Knight's guideline split into single checks; the codes and the interpretations of ambiguous wording
  are this project's (listed as questions for Knight in PLAN.md §2.9). The same legend is on the page under
  *What the rule codes mean*.
- **Conflicts, extracted data, documents, audit.** Resolve disagreements; see every value with the line and page it was
  read from (click a value for the page image); every human change with its reason.

## Configuration

All settings come from `.env` (see `.env.example`) or the environment. Defaults are in `app/config.py`.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API key (required for model calls; `ANTHROPIC_AUTH_TOKEN` also works) |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model for the AI analyst |
| `CLAUDE_EFFORT` | `medium` | Default effort for model calls |
| `CLAUDE_FALLBACKS` | `default` | Server-side refusal fallback (not sent to Haiku); empty disables |
| `LLM_MODEL_<TIER>` / `LLM_EFFORT_<TIER>` | Haiku (classify, extract), Sonnet (transcribe, draft), `CLAUDE_MODEL` (analyst) | Override the model or effort for one task tier: `CLASSIFY`, `TRANSCRIBE`, `EXTRACT`, `ANALYST`, `DRAFT` |
| `LLM_ESCALATION_MODEL` / `LLM_ESCALATION_EFFORT` | `claude-opus-5-5` / `medium` | Model used to re-run a result that failed a code check |
| `LLM_CONCURRENCY` | `6` | Page calls run at the same time inside one job |
| `LLM_TIMEOUT_SECONDS` | `180` | Timeout per model call (the SDK also retries) |
| `LLM_CACHE` | `on` | `off` sends every model request live and saves nothing (a live demo); `on` reuses saved answers to identical requests |
| `LLM_CACHE_DIR` | `llm_cache` | Response cache folder under `DATA_DIR`; point at an empty folder to measure a run with no cache hits |
| `LLM_RUN_LABEL` | — | Tag added to cost-ledger lines for an experiment |
| `DATA_DIR` | `data` | Files, page images, cache, cost ledger, SQLite database |
| `DATABASE_URL` | SQLite in `DATA_DIR` | e.g. `postgresql+psycopg://user:pass@localhost:5432/knight` |
| `RULES_FILE` | `app/rules/knight_v1.yaml` | The guideline as rules |
| `JOB_MODE` | `thread` | `thread`: the web process runs jobs; `queue`: workers (`python -m app.worker`) run them |
| `PIPELINE_ENGINE` | `code` (Docker: `graph`) | `graph`: LangGraph runs the stages, with checkpoints and review as interrupt/resume; `code`: `pipeline.run` calls the same stages in order |
| `LLM_CLIENT` | `sdk` (Docker: `langchain`) | Who sends a request on a cache miss: the Anthropic SDK or LangChain's `ChatAnthropic` (identical request) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | — / — / `https://cloud.langfuse.com` | Langfuse tracing; on when both keys are set |
| `LANGFUSE_TRACING_ENVIRONMENT` | `default` (Docker: `docker`) | Environment label on every trace |
| `LANGFUSE_CAPTURE_CONTENT` | `false` | `true` also sends document text and model outputs (PII-masked) |
| `JOB_LEASE_SECONDS` / `JOB_MAX_ATTEMPTS` / `JOB_RETRY_BACKOFF_SECONDS` / `WORKER_POLL_SECONDS` | `120` / `3` / `30` / `2` | Queue behaviour: lease length, attempts before FAILED, delay × attempt, polling interval |
| `POSTGRES_PASSWORD` | — | Docker only: password of the container's Postgres |
| `APP_PORT` | `8000` | Docker only: host port for the web app |
| `TEST_DATABASE_URL` | — | Tests only: run them against Postgres instead of a throwaway SQLite file |

Guideline thresholds (available states, day limits, ages, points, keyword lists) are not environment variables:
they live in `app/rules/knight_v1.yaml`, and every rule result records the rules version it ran under.

## Results

Each set is scored with `eval/run_eval.py` against expected values written by hand.

| Set | What it is | First run | Now |
|---|---|---|---|
| Sample (Cobalt Mesa, CA) | The case-study submission. The system was built against it, so this is a **regression check**, not an accuracy measure | n/a | **87/87** |
| Held-out #1 (Desert Ridge, AZ) | Written after the build: other carriers, a phone-photo loss run, an image-only IFTA scan, a corrupt PDF | **49/72** | 72/72 |
| Held-out #2 (Bluestem, TN) | Written after the #1 fixes: a zip with misleading names, a duplicate, a Word note, four IFTA returns in one PDF | **64/70** | 70/71 |
| Held-out #3 (Holston Valley, TN) | A **clean** 12-unit fleet: Word application, HTML-only email, a sideways phone photo of a CDL | **69/69** | 69/69 |
| Held-out #4 (Red River, TX) | The agent's email **forwarded as an attachment**; lithium batteries, cross-border "Yes", a DUI, a 67-year-old driver | **66/67** | 67/67 |

- **The first-run numbers are the measurements.** Held-out #1 and #2 were then used to fix general causes, so their
  current scores are regression results; held-out #3 has not been used for any fix; held-out #4's one miss was fixed after its first run.
- On every unseen submission, **every miss went toward "refer"**: nothing was wrongly passed or wrongly declined.
- 121 automated tests cover the guards, every rule section, reprocessing, the queue and the intake formats.

## Project structure

| Path | What |
|---|---|
| `app/intake.py`, `app/classify.py`, `app/extract/` | Intake, page classification, extraction, evidence and confidence, normalization |
| `app/pipeline.py` | One run, stage by stage; parallel page calls; IFTA reconciliation |
| `app/resolve.py`, `app/conflicts.py` | Matching entities across documents; conflicts and duplicates |
| `app/rules/` | `knight_v1.yaml` (42 rules), the engine and guards, predicates, the resolved view |
| `app/analyst.py`, `app/reply.py`, `app/commodities.py`, `app/review.py` | The AI summary; the draft reply to the agent; commodity sorting; human corrections |
| `app/graph.py` | The LangGraph graph: stages as nodes, checkpointer, human review as interrupt/resume |
| `app/tracing.py` | Langfuse traces, scores and masking |
| `app/jobs.py`, `app/worker.py` | The job queue and workers |
| `app/llm.py`, `app/db.py`, `app/config.py`, `app/log.py` | Model calls, data model, settings, logging |
| `app/web/` | FastAPI routes and templates (review page, operations page) |
| `eval/`, `tests/`, `scripts/` | Answer keys and scorer, tests, utilities |
| `docs/` | Architecture and code explanation |

## Known limitations

- Evaluated on five submissions (the sample and four held-out sets); three held-out sets have since been used for fixes. More held-out
  submissions, ideally real redacted ones, would measure accuracy with more confidence.
- SAFER violations, distance to the Mexico border and the quoted premium aren't in any submission: those checks
  are listed as "check before quoting". A 65+ driver's medical report isn't a document type the system reads.
- Ambiguities in the guideline are settings or documented interpretations, listed in PLAN.md §2.9 as questions for
  Knight.
- Single reviewer identity; no login or roles. A real mailbox, SSO and managed cloud services are designed, not built.

## How it was built

Planned, decided and reviewed by me; implemented with **Claude Code** (Anthropic) working from those decisions. Every
design choice and its reason is in DECISIONS.md, and every model run in RUN_LOG.md.
