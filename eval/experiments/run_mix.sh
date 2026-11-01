#!/usr/bin/env bash
# Per-task mix (D-31): each tier on the model its task needs, empty cache so every call is measured.
set -u
label=$1
export LLM_RUN_LABEL=$label LLM_CACHE_DIR=llm_cache_$label
export LLM_MODEL_CLASSIFY=claude-haiku-5-5   LLM_EFFORT_CLASSIFY=medium    # rules do most pages; checkable
export LLM_MODEL_TRANSCRIBE=claude-sonnet-5-5 LLM_EFFORT_TRANSCRIBE=medium  # scans/photos: weakest checks
export LLM_MODEL_EXTRACT=claude-haiku-5-5    LLM_EFFORT_EXTRACT=medium     # every value verified against the page
# analyst: CLAUDE_MODEL (Opus 5.5 medium), judgment
start=$(date +%s)
.venv/Scripts/python.exe scripts/run_sample.py > eval/experiments/${label}_run.txt 2>&1
echo "wall_seconds=$(( $(date +%s) - start ))" >> eval/experiments/${label}_run.txt
.venv/Scripts/python.exe eval/run_eval.py > eval/experiments/${label}_eval.txt 2>&1
tail -1 eval/experiments/${label}_eval.txt
.venv/Scripts/python.exe scripts/cost_report.py | grep "$label"
