# RUN LOG: every real LLM run, its cost, and what changed because of it

All runs process the same sample submission (Cobalt Mesa Transport, 15 files → 33 pages). "Eval" is
`eval/run_eval.py`: a **regression check on the sample, not an accuracy measure** (D-19).
Prices (USD per million tokens, input / output): Opus 5.5 $4 / $20, Sonnet 5.5 $2 / $10, Haiku 5.5 $0.10 / $0.50.
Since `07dea67` every model call is written to `data/llm_usage.jsonl` (task, model, effort, tokens, cost,
seconds, whether it was escalated and why), and `scripts/cost_report.py <run_id>` prints the per-task tables below.
Raw outputs of each experiment are in `eval/experiments/`.

## Summary

| # | When (UTC) | Code | Models | New calls | Wall time | Cost | Eval | Outcome |
|---|---|---|---|---|---|---|---|---|
| Run 1 | 11/01 02:21 | `22da894` | Opus 5.5 medium, every task | 43 | 561 s | **$2.37** | 52/59 | First real run. 4 gaps found, cost too high |
| Run 2 | 11/01 02:36 | `1bef35c` | same (cache hit on all pages) | 1 (analyst) | 55 s | ~$0.41 | **61/61** | Gaps fixed. Cost question raised |
| E0 | 11/01 03:56 / 03:58 | `07dea67` | same + code changes (IFTA tables in code, compact analyst input) | 1 (analyst) | ~60 s | $0.21 (analyst only) | 60/61 → **61/61** | Parser bug on Q2 total row found and fixed |
| E1 | 11/01 03:59 | `07dea67` | Opus 5.5 **low** for classify/transcribe/extract; analyst Opus medium | 36 | 667 s | **$1.39** | 61/61* | *Q1 2026 return silently lost its total (eval didn't check it) |
| E2 | 11/01 04:10 | `07dea67` | **Sonnet 5.5** medium for classify/transcribe/extract + escalation | 39 | 258 s | **$0.85** | 61/61 | Clean on the eval, but 4 of 6 loss runs lacked their line of business (found in r2) |
| E3 | 11/01 04:14 | `07dea67` | **Haiku 5.5** medium for classify/transcribe/extract + escalation | 40 | 280 s | **$0.26** | 60/61 | AL 24-25 loss run keyed `loss_run:?:…` (line of business not extracted) |
| r2 ×4 | 11/01 04:21–04:24 | `6a0b334` | E0–E3 again after the three fixes below (pages from cache) | 1 each | ~60 s | $0.20–0.22 each | **63/63 all four** | Fixes hold. Sonnet and Haiku each escalated 4 loss runs to Opus |
| **M1** | 11/01 04:36 | `3f6da31` | **Per-task mix:** classify Haiku · transcribe **Sonnet** · extract Haiku (→ Opus on failed check) · analyst Opus. **Empty cache: every call measured** | 41 (5 escalated) | 300 s | **$0.62** | **63/63** | Recommended default. 7/7 IFTA tables parsed in code |
| D1 | 11/01 04:45 | `8c030f1` | **Defaults, no overrides**, after D-31 was decided (per-task mix in `TIER_DEFAULTS`) | 7 new + 33 cached (4 escalations, from cache) | ~90 s | $0.21 (mostly the analyst) | **63/63** | Confirms the app uses the mix by default: classify/extract Haiku, transcribe Sonnet, analyst Opus |
| B3 | 11/01 04:57 / 04:58 | Block 3 commit | Defaults, through the **web app**: upload → double-start refused (409) → eval → field override → "OUT OF DATE" → rerun analysis | 1 + 1 (analyst) | ~90 s + ~60 s | $0.43 | **63/63** | Block 3 behaviour verified end to end; new rows store relative paths |
| PG1 | 11/01 07:37 | this commit | Defaults, on **PostgreSQL 18** (fresh database, D-33) | 1 new (analyst) + 39 cached | ~90 s | $0.21 | **87/87** | Same results as SQLite; web routes, overrides and job claim checked on Postgres |
| **HO1** | 11/01 08:01 | `3d9e2df` + held-out files | **Held-out submission** (Desert Ridge Freight, written from scratch after the build; D-19, D-13). Defaults, unchanged code | 26 (0 cached, 0 escalated) | ~2.5 min | **$0.18** | **49/72** | First real measurement on unseen input. Decision REFER instead of DECLINE: the DUI never reached DRV-07 (see below) |
| HO2 | 11/01 08:09 | fixes 1–4 | Held-out again after the four fixes (application pages re-extracted; the rest from cache) | 3 new + 23 cached | ~70 s | $0.16 | 69/72 | Remaining: DRV-07 and SUB-04 cited values they don't rely on (below) |
| S2 | 11/01 08:10 | fixes 1–4 | Sample again (regression check after the fixes) | 2 new + 38 cached | ~70 s | $0.24 | **87/87** | No regression |
| HO3 | 11/01 08:14 | this commit | Rules re-run on HO2 and S2 after fix 5 (no model calls) | 0 | – | $0 | **72/72** held-out, **87/87** sample | Held-out decision now DECLINE. **72/72 is a regression result, not a measurement** (the fixes were found with this file) |
| **HO4** | 11/01 16:01 | `d389d46` (unchanged) | **Held-out #2** (Bluestem Haulage, TN), written after the held-out #1 fixes, **run once** | 24 (+3 cached, 1 escalated) | ~2.5 min | **$0.17** | **64/70** | **Decision correct (DECLINE)**: tow truck and 31-35 over both caught. 6 misses, all toward NOT_EVALUATED/REFER |
| HO5 | 11/01 16:08 | this commit | Held-out #2 again after its three fixes | 1 new + 22 cached | ~45 s | $0.12 | 69/70 | Regression result (no longer a measurement). Remaining miss: no `mailing_state` field is stored; the rules derive TN from the address |
| S3 | 11/01 16:09 | this commit | Sample again (regression check) | 1 new + 39 cached | ~45 s | $0.22 | **87/87** | No regression |
| HO1 re-rules | 11/01 16:10 | this commit | Held-out #1, rules re-run only | 0 | – | $0 | **72/72** | Holds |

## Run 1: first real run (baseline)

- **Setup:** `claude-opus-5-5`, effort `medium`, for every call (D-23: measure before choosing cheaper models).
- **Result:** 43 calls, 242,150 input and 70,085 output tokens, 561 s, **$2.37**. Decision REFER → Small Fleet (correct).
- **Quality:**
  - All 13 image pages were classified correctly, and all 4 IFTA returns reconcile exactly to their printed totals.
  - 34 operating states were found, 23 of them outside Knight's list, and the LLC name in the Q4 IFTA return was caught.
  - Osei's photographed issue date 01/06/2021 was read correctly and stays LOW_CONFIDENCE (0.75) by design.
  - The analyst produced 10 concerns with every citation valid. It missed one: Osei's possible 51-day gap on an expired CDL.
- **Eval 52/59. The four gaps behind the failures (fixed in `1bef35c`):**
  1. The eval runner matched driver names wrongly.
  2. A license photo inside the MVR PDF was never compared with that MVR. The fix compares sources by (file, document type).
  3. Loss-run claims weren't checked against the schedules. The new check catches the 2021 Peterbilt and Kevin Brandt, plus Volvo vs Freightliner from the VIN's maker prefix.
  4. "Nathaniel Osei" vs "OSEI, NATHANIEL K" raised a false name conflict. The matcher now accepts a dropped middle name; INC vs LLC still conflicts.

### Where the $2.37 went (Run 1, per task)

| Task | Calls | Input tok | Output tok | Cost | Share | What it is |
|---|---|---|---|---|---|---|
| extract_ifta_return | 11 | 55,175 | 29,030 | $0.80 | 34% | ~600 table cells re-typed as JSON, one excerpt per cell (~2,600 output tokens per call) |
| classify_vision | 13 | 51,362 | 12,610 | $0.46 | 19% | Scans and photos: classify + transcribe |
| analyst | 1 | 75,688 | 5,209 | $0.41 | 17% | Input included every IFTA row field |
| extract_mvr | 6 | | | $0.27 | 11% | |
| extract_loss_run | 6 | | | $0.17 | 7% | |
| extract_quote_form | 1 | | | $0.11 | 5% | |
| extract_application | 1 | | | $0.09 | 4% | |
| extract_driver_license | 2 | | | $0.06 | 3% | |
| classify_text | 2 | | | $0.01 | <1% | 26 of 33 pages were classified by rules, at no cost |
| **Total** | **43** | **242,150** | **70,085** | **$2.37** | | |

**Why this was too much:** at roughly 500 submissions a day, $2.37 is about $1,200 a day, or about $430k a year. More than half of it paid a frontier model for work that needs no judgment: re-typing a table the vision step had already transcribed, and reading 600 numbers in order to "notice" totals that code can add exactly.

## Run 2: re-run after the gap fixes

- Every page came from the on-disk cache, keyed by a hash of each request. Only the analyst ran again, because its input changed. 55 s, about $0.41.
- **Eval 61/61.** Showed that the cache makes reprocessing nearly free and repeatable.

## Decisions taken on cost (D-31 in DECISIONS.md)

Ordered as decided: free code changes first, then measured model changes.

1. **IFTA tables are read by code, not re-extracted by the LLM.**
   - The vision transcript already lays tables out one row per line, with cells separated by `|`.
   - `app/extract/table_transcript.py` maps columns by header synonyms, as the XLSX schedules do. It carries the header onto continuation pages and handles a dropped all-empty Surcharge column. Each row line becomes the excerpt for its values.
   - If a page doesn't parse cleanly (no header, unexpected cell count, a non-US jurisdiction), it returns nothing and the LLM extractor runs instead.
   - The summary pages (penalty, interest, taxpayer name) stay on the LLM, because a code parser for one state's form layout is exactly what D-02 avoids.
   - Values keep the vision prior (0.75), because the transcription is still model output. They are confirmed only by the rows-sum-to-total check.
2. **The analyst gets aggregates computed in code instead of raw IFTA rows.**
   - It now receives per-quarter totals, whether each quarter's rows reconcile, and miles by state across all quarters.
   - Each source file is named once (`S1`, `S2`…) instead of on every field, and the JSON is compact.
   - Effect: 145k → 56k characters, 76k → 28k input tokens, $0.41 → $0.21.
   - Its citations still point at real field ids: each quarter carries its `total_field` id.
   - The aggregates are deterministic. The analyst's judgment is not: it is still an LLM call, cached by the hash of its input. See the note below.
3. **Per-task model routing with escalation.** Tiers: `classify`, `transcribe`, `extract`, `analyst`. Each one's model and effort can be set with `LLM_MODEL_<TIER>` / `LLM_EFFORT_<TIER>`, and every tier defaults to `CLAUDE_MODEL`.
   - When a tier runs on a cheaper model, its result is checked in code. A failed check re-runs that page on `LLM_ESCALATION_MODEL` (Opus 5.5).
   - Extraction escalates on a model error, or when more than 20% of a page's excerpts can't be found on the page.
   - Transcription escalates on a model error, an "unreadable" verdict, or an IFTA return whose rows don't add up to its printed total. In that case the whole document is re-transcribed and re-extracted.
   - Principle: *verification is what makes a cheap model safe.* A cheaper model can cost an extra call, but it can't silently hand the rules a wrong value that a check would catch.
   - Haiku 5.5 has no server-side refusal fallback, so `fallbacks` is not sent for it. A refusal becomes a model error, which escalates.
4. **The analyst stays on Opus 5.5.** It is one call and pure judgment, the place where model quality shows most.
5. **A cost ledger** (`data/llm_usage.jsonl`) and `scripts/cost_report.py`, so every claim in this file can be checked.

### Note: "the summarized analyst input is deterministic"

- **Partly right.** The summaries themselves (miles by state, quarter totals, TIV, CDL evidence, operating states) are computed in code. They are deterministic, exact and identical on every run.
- **The analyst's output is not deterministic.** It is still a model call that applies judgment: which concerns matter, how severe they are, what to ask the agent.
- What changed is the division of labor. Arithmetic moved to code, which never adds wrong. The model reads 28k tokens of facts instead of 76k tokens it would otherwise have to add up itself.
- Repeatability comes from the cache: the same input gives the same cached answer. A fresh call may word things differently, and its citations are validated in code either way.

## E0: code changes only (Opus 5.5 medium everywhere)

- All 7 IFTA table pages were parsed in code (`IFTA table parsed in code` in the job log). The 4 summary pages still went to the LLM, served from cache.
- **First run 60/61:** Q2 2026 lost its total miles. On that page the total row keeps its empty leading cells (` | | | 103,773 | …`), so the first cell was blank.
- **Fix:** a full-width total row is read from the "Total Miles" column. A regression test was added. Re-run: **61/61**, with all 4 returns reconciling.
- **Analyst:** 28,244 input and 4,760 output tokens, $0.21. 9 concerns, 0 unsupported citations.
- **Estimated full fresh-run cost for E0:**
  - $2.37, less the IFTA table calls (7 of 11, about $0.55–0.65), less the analyst saving ($0.20), comes to **about $1.5–1.6**.
  - This is an estimate, because E0's pages came from cache. E1–E3 below are measured from fresh calls.

## E1–E3: model experiments (fresh calls, measured)

- **Same for every variant:** the analyst stayed on Opus 5.5 medium. Only the `classify`, `transcribe` and `extract` tiers changed.
- **Approval:** the experiments were approved by the user ("implement the fixes and also do the experiments").
- **Spend:** about $2.50 for E1–E3, plus about $1.10 for the six cached re-runs, which paid only for the analyst.

### Per-task cost, fresh run

| Task | Run 1 Opus medium | E1 Opus low | E2 Sonnet 5.5 | E3 Haiku 5.5 |
|---|---|---|---|---|
| classify_vision (13 scans/photos) | $0.46 | $0.454 | $0.230 | $0.012 |
| extract_ifta_return | $0.80 (11 calls) | $0.104 (4) | $0.114 (7) | $0.013 (8) |
| extract_mvr (6) | $0.27 | $0.224 | $0.110 | $0.007 |
| extract_loss_run (6) | $0.17 | $0.152 | $0.070 | $0.006 |
| extract_quote_form | $0.11 | $0.111 | $0.054 | $0.004 |
| extract_application | $0.09 | $0.063 | $0.031 | $0.003 |
| extract_driver_license (2) | $0.06 | $0.062 | $0.030 | $0.002 |
| classify_text (2) | $0.01 | $0.012 | $0.006 | $0.000 |
| analyst (Opus medium in all) | $0.41 | $0.212 | $0.205 | $0.209 |
| **Total** | **$2.37** | **$1.39** | **$0.85** | **$0.26** |
| + escalations at fresh prices (4 loss runs on Opus, about $0.028 each) | — | — | about +$0.11 | about +$0.11 |
| **Expected per submission** | $2.37 | $1.39 | **about $0.96** | **about $0.37** |

### Observations

- **Opus at low effort barely saves anything.** Transcription cost the same ($0.454 against $0.46): reading a scan takes output tokens whatever the effort. It was also the variant that produced the silent Q1 miss. **Rejected.**
- **IFTA tables parsed in code** varied by model: 7 of 7 pages on Opus, 4 on Sonnet, 3 on Haiku. Each model lays its transcript out slightly differently (column order, dropped empty cells), and the parser then refuses rather than guess. The LLM extractor handled the remaining pages, and every return reconciled exactly on all variants.
- **Scan-derived values held on Haiku:** 34 operating states, the LLC name in Q4, and Osei's photographed CDL issue date 01/06/2021. The eval checks all three.
- **Low-confidence counts** were similar on every variant (21–25 values), so cheaper models didn't produce more uncertain values on this sample.
- **Haiku's output-token count is higher** (69k against 38–41k), because it is more verbose, but at $0.50 per million that is irrelevant.

### What the experiments found, and the fixes (r2)

Neither cheap-model failure was a wrong number. Both were **missing** values, which the existing checks didn't catch:

1. **A continuation page was never extracted.**
   - Opus at low effort labelled Q1 2026 p3 a "continuation of the IFTA return". The pipeline recorded which document it continued, but still skipped extracting it, so the return lost its rows and its printed total.
   - **Fix:** a continuation page is extracted as the document type it continues. Pages the content rules mark as a report trailer ("END OF REPORT FOR…") stay skipped.
   - This was a pipeline bug, not a model one, and could have hit any model.
2. **A return with no printed total passed silently.**
   - The rows-sum-to-total check only ran when a total existed.
   - **Fix:** a return with no total, or no rows, now fails the check. A cheap tier re-transcribes it on Opus; otherwise the job log warns that the values stay LOW_CONFIDENCE.
3. **Key fields went missing on cheap models.**
   - Haiku left out the loss run's line of business, so the AL policy was keyed `loss_run:?:…`. The re-run showed Sonnet does the same on 4 of 6 loss runs. The loss runs print "COVERAGE AUTO LIABLITY" (sic), and mapping that to AL is a small inference that cheaper models skip.
   - **Fix:** `KEY_FIELDS` per document type (loss run: line and policy number; license: license number). A cheap-tier page missing any of them escalates to Opus.
4. **Eval gaps:** the expected file now also checks the Q1 2026 and Q4 2025 IFTA totals. It had checked only 2 of the 4 quarters, which is why E1's miss scored 61/61. The check count goes from 61 to 63.

After the fixes, all four variants scored **63/63**. In r2, Sonnet and Haiku each escalated exactly the 4 loss runs that lacked their line of business. Those escalated calls were served from cache, because E0 had already run the same pages on Opus, so their fresh price is added to the table above.

### Caveats

- **One sample:** this is a regression check on the very sample the system was built against (D-19). Before trusting a cheaper model, it needs to pass the held-out set as well.
- **Escalation rate:** in production, this is the number to watch. Here it was 4 of 40 calls. A rising rate means the cheap tier is drifting, and as it rises the savings shrink toward Opus prices.
- **A possible follow-up:** sharpening the `line` field description, for example "map the coverage heading to AL / MTC / APD", would likely remove the 4 escalations. That changes every extraction request, so it would need a fresh measured run.

### Recommendation (D-31, PROPOSED: your call)

- **Haiku 5.5** for `classify`, `transcribe` and `extract`, with escalation to Opus on any failed check.
- **Opus 5.5 medium** for the analyst.
- Expected cost is **about $0.37 per submission, down from $2.37 (−84%)**.
- **Safer alternative:** Sonnet 5.5 at about $0.96 (−60%).

Both scored 63/63 and passed every scan-derived check. Haiku's mistakes were all omissions, which the checks catch and escalate, rather than wrong values.

Raw outputs of every run are in `eval/experiments/`. They stay local and out of git, because they echo the sample's case data.

## Review: "is this production-grade, or patchwork?" (user question, 11/01)

- **The user's pushback was right on two counts:**
  - The experiments varied all three mechanical tasks together, so they were a model swap, not a per-task choice.
  - The IFTA parser was fitted to one model's transcript layout.
- **Honest assessment:**
  - **General, not fitted to the sample:** one schema-driven extractor, evidence checked in code, deterministic YAML rules, conflicts surfaced rather than auto-picked, missing or uncertain values giving REFER rather than PASS, and escalation triggered by failed checks rather than by file names.
  - **Sample-specific:** the IFTA column vocabulary (one state's form), the `KEY_FIELDS` list, the 20% unverified threshold (a guess), and the eval (one sample, with no held-out set yet).
- **Principle for choosing a model per task:** can code check the task's output?
  - Text classification and text extraction are checkable: the excerpt must be on the page and contain the value. They run on **Haiku**.
  - Transcribing scans and photos is only partly checkable. IFTA tables reconcile to their totals, but a photographed license date has nothing to check against, and the evidence check runs on the transcript itself. It runs on **Sonnet**.
  - The analyst exercises judgment, so it stays on **Opus**.

### IFTA parser hardening

Every stored transcript from all four models (28 table pages) was replayed through the parser. **Before:** 7/7 pages on Opus, 4/7 on Sonnet, 3/7 on Haiku. The variants it had failed on:

| Variant (model) | Before | Now |
|---|---|---|
| Key/value line written with a pipe, `Account Number: \| 247-918305` (Sonnet) | whole page rejected | line skipped as non-table |
| Total row labelled `Totals \| \| 142,087 …` (Haiku) | read as a bad jurisdiction, page rejected | recognized as the total row |
| Header written as `Tax-Paid Gallons`, `Total Due` and so on | would miss the column | columns matched by meaning |
| Whole page run together on one line (Haiku, Q3 2025) | rejected | **still rejected on purpose**: the LLM extractor reads it |

- **Result:** 27 of 28 pages now parse, and every parsed return reconciles exactly to its printed total.
- **Fails closed:** a row the parser skips or misreads cannot pass silently, because the rows must add up to the printed total. A return that doesn't is re-transcribed on Opus or left LOW_CONFIDENCE.
- **Tests:** 36 pass, including the layout variants above.

### M1: per-task mix, fully measured (empty cache)

| Task | Model | Calls | Cost | Share |
|---|---|---|---|---|
| classify_vision (13 scans/photos) | Sonnet 5.5 | 13 | $0.230 | 37% |
| analyst | Opus 5.5 | 1 | $0.219 | 35% |
| extract_loss_run | Haiku, **5 escalated to Opus** | 11 | $0.155 | 25% |
| extract_mvr | Haiku | 6 | $0.007 | 1% |
| extract_quote_form | Haiku | 1 | $0.004 | 1% |
| extract_application | Haiku | 1 | $0.004 | 1% |
| extract_ifta_return (summary pages only; tables parsed in code) | Haiku | 4 | $0.003 | <1% |
| extract_driver_license | Haiku | 2 | $0.002 | <1% |
| classify_text | Haiku | 2 | $0.000 | 0% |
| **Total** | | **41** | **$0.622** | |

- **Quality:** eval 63/63, all 4 IFTA returns reconcile, and 25 values are LOW_CONFIDENCE (21–25 on every other variant). 300 s of wall time.
- **Cost:** $0.62 per submission is **−74% against Run 1 ($2.37)**. At 500 a day, that is about $310 a day instead of $1,185.
- **Where the rest of the money goes:**
  - *Transcription and the analyst* account for 72%, and both stay on stronger models on purpose.
  - *Escalations* account for 25%: 5 of 6 loss runs, because Haiku omits the line of business. Mapping the printed "COVERAGE AUTO LIABLITY" (sic) to AL is a small inference Haiku skips.
  - *Next lever, not built:* derive `line` in code from the printed coverage heading (AUTO LIABILITY → AL, MOTOR TRUCK CARGO → MTC). That would remove the escalations, about −$0.15, for **about $0.47**. It changes extraction, so it would need another measured run.

### Still needed before calling the cheap configuration production-ready

1. **A held-out set**, the open item from D-19: other carriers' loss runs, another state's IFTA return, phone photos and a bad scan, all built from scratch rather than edited copies of this sample. Calibrate the 20% threshold and the confidence priors on it.
2. **Shadow mode in production:** run the mix and Opus side by side on a slice of real traffic, and switch when they agree.
3. **Monitor the escalation rate.** A rising rate means the cheap tier is drifting.

## Decision (11/01)

The user approved the per-task mix as the default. D-31 is now DECIDED and implemented in `app/config.py` (`TIER_DEFAULTS`). To compare against Opus everywhere, set `LLM_MODEL_<TIER>=claude-opus-5-5`.

## Held-out measurement (HO1, 11/01)

**Why:** every number above is a regression check on the sample the system was built against (D-19). This is the
first run on a submission written from scratch afterwards and never used to tune the code. It is a fictitious Arizona
carrier: loose attachments with misleading names, other carriers' loss runs (one as a tilted, blurred phone photo),
Arizona IFTA returns (one an image-only scan with abbreviated headers), a corrupt PDF, and a roster spreadsheet with
different headers. Expected values were written by hand **before** the first run (`eval/heldout/desert_ridge_expected.json`);
the generator is `scripts/make_heldout.py`.

**Result: 49/72. Decision REFER, expected DECLINE.** It cost $0.18 for 26 calls.

**What generalized:**
- every page classified correctly by content, including `scan001.pdf` → loss run and the photo → loss run;
- the corrupt PDF became UNREADABLE with its error shown;
- FEIN, DOT, MC, dates and coverage answers were read correctly;
- all three IFTA totals were right, including the scan with abbreviated headers, and the box-truck VIN check passed;
- the DUI was read correctly from the MVR (02/14/2024, 8 points);
- the commodity list was recognized as complete (60 + 25 + 15).

**What broke (4 root causes, all generality gaps, no flaw in the expected file):**

1. **Roster headers.** "Driver Name" and "CDL #" are not in the spreadsheet parser's header list, so the three driver rows
   became loose notes. With no driver schedule, SUB-06, SUB-07 and DRV-01..08 failed or were not evaluated, the DUI
   never reached DRV-07 (**a missed DECLINE**), and the DOB mismatch was never compared. The general flaw: when the
   deterministic parser recognizes no key columns, it gives up silently instead of falling back to the model extractor.
2. **Loss-run line not normalized.** The line of business went into the entity key as printed ("COMMERCIAL AUTO LIABILITY",
   "AUTO LIABILITY (AL)") instead of AL, so SUB-04 counted 0 prior AL years instead of 3.
3. **"Body Type" not mapped** to vehicle type, so the box truck was never seen and SEL-02 passed instead of referring.
4. **Prior insurance on an application.** The catalogue expects a prior-insurance table only on a quote form (as in the
   sample), so the application's table was not extracted. PRI-01..03 then relied on loss-run terms, one of them from the
   photo. Photo values start below the review threshold by design, and with nothing to confirm them the C4 guard
   correctly refused to PASS.

**Direction of every error: toward REFER, never toward a wrong PASS or a wrong DECLINE.** The guards (unread ≠ missing,
low confidence never passes, a decline needs cited evidence) held on unseen input. The cost of the gaps is a missed
decline routed to a person, not a bad account bound.

**Honesty note:** once these gaps are fixed using this file, it is no longer a clean held-out set. 49/72 stays the
measurement; a re-run after fixes is a regression check on it, and a new measurement needs a second held-out submission.

### Fixes from the held-out set (HO2, HO3)

Each fix targets the general cause, not this file's exact wording:
1. **Spreadsheet fallback.** When a sheet has a header row but no row can be identified as a driver or a unit, the
   parser returns nothing, so the model extractor reads the sheet. Before, it filed the rows as notes silently.
   Also added common header variants: "Driver Name", "CDL #", "DL #", "Body Type", "Type", "Yr", "Stated Amount".
   **Caveat:** the vocabulary additions were informed by this file.
2. **Line of business as a code.** "COMMERCIAL AUTO LIABILITY" and "AUTO LIABILITY (AL)" map to AL, cargo to MTC,
   physical damage to APD. Unrecognized text (e.g. General Liability) maps to nothing; it is never guessed.
3. **Prior insurance on applications.** The application catalogue now has the prior-insurance table the quote form
   already had.
4. **A listing confirms a loss run.** A loss-run policy number, start or end that matches a term listed on the
   application or quote is confirmed: two independent documents agree. This lets a photographed loss run count once
   the typed listing backs it.
5. **A rule cites only what it relies on.** This is the principle first applied to driver points. DRV-07 had cited
   the violation's event type ("VIOL"), a label the model inferred from a section heading. It wasn't printed on the
   row, so it was low-confidence, and the guard downgraded a well-evidenced DECLINE. SUB-04 had cited the photo's
   carrier name. Both now cite only the description and date, and the term dates.

**Result:** held-out 72/72 with decision DECLINE (the DUI), and the sample still 87/87. **The held-out number that
counts is 49/72 (HO1).** A new measurement needs a second held-out submission, not built.

## Held-out #2 measurement (HO4, 11/01)

**Built after the held-out #1 fixes and run once on unchanged code (`d389d46`), so 64/70 is a measurement.**
Expected values were written first (`eval/heldout/bluestem_expected.json`); the generator is `scripts/make_heldout2.py`.

**Correct:**
- the decision (DECLINE, from two independent planted declines);
- the zip unpacked, the duplicate application flagged DUPLICATE, the .docx flagged UNSUPPORTED;
- a misleadingly named loss run classified by content;
- the PDF driver and vehicle schedule (no spreadsheet this time) read;
- the 22-year-old driver, the 121-day-old MVR, the AL deductible, the APD flag, the new-venture corporation, the missing
  stated value, the speeding points, the short coverage history, the missing loss run and the HAULAGE/HAULING conflict;
- the 90% commodity list correctly treated as incomplete for PRO-02/PRO-08.

**Misses (3 causes, 6 checks; every one toward NOT_EVALUATED/REFER, none toward a wrong PASS or DECLINE):**
1. **Several IFTA returns in one PDF, one scanned (2 checks).** The scanned page's table was read in code, but its quarter
   was taken from the previous page, because a document's IFTA period carries over to its later pages (true for the
   sample, where page 2 continues page 1's return). So the Q1 2026 rows were filed under Q4 2025, Q4 stopped reconciling
   (an Opus re-transcription didn't help, since transcription wasn't the problem), and Q1 2026 counted as missing:
   SUB-05 refers.
2. **State inside a one-line address (3 checks).** "418 Industrial Pkwy, Jackson, TN 38301": the model didn't fill a
   separate state field, so ST-01 and ST-03 are NOT_EVALUATED. This is safe (refer), but the state is right there.
3. **An explicit "No" ignored (1 check).** "Towing or recovery operations: No" was answered, but PRO-06 only uses the
   answer when it is Yes. With the commodity list incomplete (90%), it became NOT_EVALUATED, inconsistent with PRO-01 and
   PRO-07, where an explicit No passes.

**Summary across both held-out sets:** 49/72 (HO1) and 64/70 (HO4), both on unseen input. On the second, every hard
decision was right; the misses were all conservative.

### Fixes from held-out #2 (HO5)

1. **A page that names its own quarter is its own return.** The IFTA table parser reads the period from lines like
   "Quarter: 01/01/2026 through 03/31/2026" (ignoring the due date on the same line) and keys the page's rows to it,
   instead of inheriting the previous page's quarter. This fixes several returns in one PDF.
2. **Domicile state from a one-line address.** When the state field is empty, the rules read the state in code from
   the mailing or garaging address (", TN 38301"), checked against the US state list and cited as evidence. The
   extracted field itself is still empty: the eval check for `mailing_state` stays a miss, honestly.
3. **An explicit "No" to towing passes PRO-06**, as it does for hazmat and intermodal, unless a commodity says towing.

**Result:** held-out #2 69/70, held-out #1 72/72, sample 87/87; 58 tests. The measurements that count stay
**49/72 (HO1) and 64/70 (HO4)**. Both held-out sets have now been used for fixes, so a new measurement needs a third.

### Code-review fixes (CR1, 11/01, no model calls)

`/code-review high build` found ten issues; all ten fixed (D-35). Verification without spending: 65 tests pass (7 new),
and the rules were re-run on the stored submissions (sample #7, held-out #3 and #6): **87/87, 72/72, 69/70**, with no
rule outcome or cited-value count changed. The extraction-time fixes (IFTA re-read keeps continuation pages, empty
cheap-tier extraction escalates, human decisions re-applied after the re-read, separate keys for policies sharing a
term) are tested with synthetic pages; a fresh pipeline run would exercise them on real input.

### Reprocess after the code-review fixes (CR2, 11/01)

Held-out #1 (Desert Ridge, submission 3) reprocessed end to end on the fixed code, run `sub3-20261101T163634-cr1-reprocess`.
**72/72, DECLINE, Small Fleet**, as before. Cost **$0.143**: every extraction call was a cache hit (same model output as
HO2), and the only new call was the AI analyst on Opus. No escalations fired: the image-only IFTA return
(`fuel_tax_Q1.pdf`, 51 values) reconciled to its printed total on the first pass, so all 51 values went from
LOW_CONFIDENCE to OK. No page came back empty. So the run confirms the fixed code runs cleanly on real input, but this
submission doesn't trigger the IFTA re-read, empty-page escalation or same-term policy cases; those stay covered by
`tests/test_review_fixes.py` only.

### AI summaries regenerated (CR3, 11/01)

The rules-version change from the review fixes marked two AI summaries out of date (the decisions didn't change). The
analyst was re-run on Opus for the sample (#7, $0.209) and held-out #2 (#6, $0.113): **$0.32 total**. Both are current
(no error, no out-of-date flag); sample 87/87, held-out #2 69/70.

### Docker (DK1, 11/01)

`docker compose up --build`: app and Postgres 18 containers both healthy. The sample email was uploaded through the web
form (`POST /submissions`) to the container app and processed end to end on the container's own database: **87/87,
REFER, Small Fleet**, $0.217 (39 cache hits from the mounted `data/`, one new analyst call). 65 tests pass inside the
container.

### Parallel page calls and the job queue (QU1, 11/01)

- **Parallel path, sample (local):** reprocessed with `LLM_CONCURRENCY=6`: **87/87**, 39 of 40 calls cache hits, one
  analyst call $0.224.
- **Queue in Docker** (app, 2 workers, Postgres 18): the two held-out emails uploaded at once; the job table showed one
  RUNNING job per worker. One worker was killed (`docker kill`) mid-run. When its lease ran out (20:13:44), the other
  worker logged "stopped renewing its lease; job taken over", re-ran the job (attempt 2) and finished it. Results:
  Desert Ridge **72/72**, Bluestem **69/70**, $0.262 together (the killed attempt cost $0: all cache hits).
- 71 tests pass, locally and inside the container.
- The speed-up itself is not measured yet: these runs were mostly cache hits, so their time says nothing about model
  wait. A run on an empty cache (about $0.62) would measure it.

### Parallel page calls, measured (PAR1, 11/01)

The sample on an **empty cache** (`LLM_CACHE_DIR=llm_cache_par1`), per-task models, `LLM_CONCURRENCY=6`, compared with
M1 (the same models and sample, one call at a time):

| | M1 (sequential) | PAR1 (6 at a time) |
|---|---|---|
| Model calls | 41 | 40 (all fresh) |
| Time spent in model calls | 290 s | 267 s |
| **Wall time** | **290 s** | **97 s (-67%)** |
| Cost | $0.622 | $0.552 |
| Eval | 87/87 | 87/87 |

By tier in PAR1: transcription 13 calls (73 s of model time, $0.229), extraction 24 (149 s, $0.110; 4 loss-run pages
escalated to Opus for a missing line of business, as in earlier runs), classification 2, analyst 1 (43 s, $0.213). The
total model work is about the same; it now overlaps. The analyst is now 43 of the 97 seconds: it reads every value, so
it can't start earlier. The cost difference is run-to-run variation in output length, not a saving from parallelism.

### Handwriting (HW1, 11/01)

A phone photo of a handwritten agent note (`samples/handwritten_note.jpeg`, written by hand for this test, fictitious
details; not in git) uploaded on its own to the Docker app (queue mode). Expected values written down before the run:
Maria Lopez, DOB 04/12/1998, CDL Class A, CA license B4419027, hired 9/2026, 6 years CDL experience.

- Transcript (Sonnet): "Pls add driver - Maria Lopez; DOB 04/12/1998, CDL Class A, CA lic B4419027, hired 9/2026, 6 yrs
  CDL exp. MVR to follow - Dave". Word for word, including the hard-to-read first name.
- Classified DRIVER_SCHEDULE (confidence 0.55, just above the 0.5 cut-off: a free-form note is not a schedule layout).
- **10 of 10 values correct**, keyed to the license number; every excerpt verified on the page. All LOW_CONFIDENCE
  (0.75): a handwritten photo can't be confirmed by code, so the guard turns rules that rely on them into "needs review".
- Decision REFER: the AI summary says the package holds only this note and lists what's missing. Cost **$0.068**
  (3 calls: transcription, extraction, analyst).

### Word files read (DX1, 11/01)

Held-out #2's `notes.docx` ("Agent notes: insured is growing fast, wants quote by 10/15") is now read at intake
(D-37). First reprocess: read, classified OTHER at **0.45**, so it was marked UNCLASSIFIED and PRI-03 said "some
loss-run pages could not be read" instead of REFER (69/71). Cause: the classifier was never told what confidence means
for the catch-all type. After clarifying it (text classifier only): **OTHER at 0.90**, page read and skipped (nothing
to extract), PRI-03 REFER again.

All three re-run in Docker through the queue (2 workers): sample **87/87**, held-out #1 **72/72**, held-out #2
**70/71** (the remaining miss is the `mailing_state` field, as before). Cost $0.48 for the three: the text-classification
calls were new because the prompt changed (Haiku), the scans reused their saved transcriptions, plus the analyst.

### Email body and photos (ML1, 11/01)

The three emails uploaded fresh to the Docker app (the body is read at intake, so older submissions don't have it).
Each body became `email-body.txt`, classified OTHER (0.95, 0.97, 0.95) and skipped for extraction; the analyst
received it as context. Desert Ridge's `IMG_4471.jpg` was already within limits, so it was used as-is and its saved
transcription applied. Scores: sample **87/87**, held-out #1 **72/72**, held-out #2 **70/71** (same `mailing_state`
miss). Cost $0.49 (two new calls each: the body's classification and the analyst).

### Held-out #3: a clean fleet (CL1, 11/01)

Built to test what no set had reached (D-39): a submission that should pass every rule, and Fleet routing. Holston
Valley Freight Inc (Knoxville, TN), 12 tractors, 12 drivers with clean MVRs, four contiguous clean AL loss runs,
four IFTA quarters, no deductible, no APD, a commodity list totalling 100%. New formats: the application as a `.docx`
with tables, an HTML-only email, loose attachments (no zip), the vehicle schedule named `Book1.xlsx`, and a phone
photo of a CDL saved sideways with an EXIF rotation tag. Expected values (`eval/heldout/holston_expected.json`) were
written before `scripts/make_heldout3.py` generated the files; run once, in Docker through the queue.

**Result: 69/69 on the first run.** Decision **QUOTE_AFTER_CHECKS** (38 rules PASS, RTE-01 routes to **Fleet**, and
only PRO-03, PRO-05, SEL-03 open as "check before quoting"), no conflicts, every document classified correctly, and
the sideways CDL photo turned upright and read (DOB, expiration match the MVR). Cost $0.193 (42 calls, none cached).

Then the AI summaries of the other four submissions, marked out of date by the rules-version change, were
regenerated (all current).

### Review-page changes and reprocesses (RV1, 11/02)

No rule, outcome or score changed (D-40 to D-43 are intake and display changes). Model calls made:

| Run | What | Calls (cached) | Cost |
|---|---|---|---|
| `sub9-20261102T050159` | Your dry-run upload of the sample (#9) | 41 (40) | $0.237 |
| `sub5-…T052454`, `sub9-…T052454` | Reprocess #5 and #9 so their conflicts use the new plain sentences | 41 (40) each | $0.223 + $0.234 |
| `sub7-…T231040` + `sub7-…T231749` | The 11/01 summary regeneration for #7 ran twice: a container rebuild killed the worker mid-call, and the other worker took the job over when its lease expired (the takeover shown in `notes/demo.sql`) | 1 + 1 | $0.121 + $0.110 |

The other summary regenerations of 11/01 (after D-39): #4 $0.051, #5 $0.234, #6 $0.136. Rules were re-run on
every submission for the new message wording (no model calls). Sample #5 still 87/87; 83 tests pass.
