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
| **M1** | 11/01 04:36 | this commit | **Per-task mix:** classify Haiku · transcribe **Sonnet** · extract Haiku (→ Opus on failed check) · analyst Opus. **Empty cache: every call measured** | 41 (5 escalated) | 300 s | **$0.62** | **63/63** | Recommended default. 7/7 IFTA tables parsed in code |

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
