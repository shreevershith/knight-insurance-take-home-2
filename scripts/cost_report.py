"""Cost per run from the LLM ledger (data/llm_usage.jsonl), broken down by task.

Usage: .venv/Scripts/python.exe scripts/cost_report.py            # every run, one line each
       .venv/Scripts/python.exe scripts/cost_report.py <run_id>   # per-task table for one run
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402


def load() -> list[dict]:
    if not config.USAGE_LOG.exists():
        return []
    return [json.loads(line) for line in config.USAGE_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    rows = load()
    runs = defaultdict(list)
    for r in rows:
        runs[r["run_id"]].append(r)
    if len(sys.argv) == 1:
        print(f"{'run':45} {'calls':>5} {'cached':>6} {'escal':>5} {'in tok':>8} {'out tok':>8} {'cost $':>7}")
        for run, items in runs.items():
            live = [r for r in items if not r["cache_hit"]]
            print(f"{run:45} {len(live):5} {len(items) - len(live):6} {sum(r['escalated'] for r in live):5} "
                  f"{sum(r['input_tokens'] for r in live):8} {sum(r['output_tokens'] for r in live):8} "
                  f"{sum(r['cost_usd'] for r in live):7.3f}")
        return
    items = [r for r in runs[sys.argv[1]] if not r["cache_hit"]]
    by_task = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "models": set(), "escalated": 0})
    for r in items:
        t = by_task[r["task"]]
        t["calls"] += 1
        t["in"] += r["input_tokens"]
        t["out"] += r["output_tokens"]
        t["cost"] += r["cost_usd"]
        t["models"].add(f"{r['model'].replace('claude-', '')}/{r['effort']}")
        t["escalated"] += r["escalated"]
    total = sum(t["cost"] for t in by_task.values()) or 1
    print(f"| Task | Calls | Escalated | Input tok | Output tok | Model/effort | Cost $ | Share |\n|---|---|---|---|---|---|---|---|")
    for task, t in sorted(by_task.items(), key=lambda kv: -kv[1]["cost"]):
        print(f"| {task} | {t['calls']} | {t['escalated']} | {t['in']:,} | {t['out']:,} | {', '.join(sorted(t['models']))} "
              f"| {t['cost']:.3f} | {t['cost'] / total:.0%} |")
    print(f"| **Total** | {len(items)} | | | | | **{total:.3f}** | |")
    for r in items:
        if r["escalated"]:
            print(f"  escalated {r['task']}: {r['escalation_reason']}")


if __name__ == "__main__":
    main()
