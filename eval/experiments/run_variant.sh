#!/usr/bin/env bash
# Usage: run_variant.sh LABEL MODEL EFFORT  -> classify/transcribe/extract tiers on MODEL/EFFORT, analyst default
set -u
label=$1; model=$2; effort=$3
export LLM_RUN_LABEL=$label
for t in CLASSIFY TRANSCRIBE EXTRACT; do export LLM_MODEL_$t=$model LLM_EFFORT_$t=$effort; done
start=$(date +%s)
.venv/Scripts/python.exe scripts/run_sample.py > eval/experiments/${label}_run.txt 2>&1
echo "wall_seconds=$(( $(date +%s) - start ))" >> eval/experiments/${label}_run.txt
.venv/Scripts/python.exe eval/run_eval.py > eval/experiments/${label}_eval.txt 2>&1
tail -1 eval/experiments/${label}_eval.txt
