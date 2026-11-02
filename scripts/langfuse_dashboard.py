"""Create the Langfuse dashboard for Knight Intake (D-52), from the traces and scores app/tracing.py sends.

Three rows, three questions:
  1. What does a submission cost, and where does the money go?   (cost per run, cost by task and model, run time)
  2. Can we trust what the AI read?                                (our code checks, as scores per run)
  3. Where do people step in?                                      (corrections by field, review actions, decisions)

Uses the Langfuse dashboards API (marked unstable by Langfuse) with the keys in .env. Each run creates a new
dashboard; delete old ones in the Langfuse UI.

Usage: .venv/Scripts/python.exe scripts/langfuse_dashboard.py
"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
AUTH = (os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", ""))


def score(name):
    return {"column": "name", "operator": "=", "value": name, "type": "string"}


def scores(*names):
    return {"column": "name", "operator": "any of", "value": list(names), "type": "stringOptions"}


GENERATIONS = {"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}
RUNS = {"column": "name", "operator": "=", "value": "process-submission", "type": "string"}

# (row, name, description, view, chartType, metrics, dimensions, filters)
WIDGETS = [
    # 1. cost
    (0, "Cost per submission run (USD)", "Average model cost of one pipeline run (score run-cost-usd; cache hits cost nothing)",
     "scores-numeric", "NUMBER", [{"measure": "value", "agg": "avg"}], [], [score("run-cost-usd")]),
    (0, "Model cost by task", "Where the money goes: total cost of each kind of model call",
     "observations", "HORIZONTAL_BAR", [{"measure": "totalCost", "agg": "sum"}], [{"field": "name"}], [GENERATIONS]),
    (0, "Model cost by model", "Haiku reads, Sonnet transcribes and drafts, Opus analyses and re-reads failures",
     "observations", "PIE", [{"measure": "totalCost", "agg": "sum"}], [{"field": "providedModelName"}], [GENERATIONS]),
    (0, "Pipeline run time (p95, ms)", "How long a submission takes from upload to decision",
     "observations", "LINE_TIME_SERIES", [{"measure": "latency", "agg": "p95"}], [], [RUNS]),
    # 2. trust
    (1, "Quoted lines found on the page", "Share of AI-read values whose quoted source line was verified on the page (1.0 = all)",
     "scores-numeric", "LINE_TIME_SERIES", [{"measure": "value", "agg": "avg"}], [], [score("evidence-verified-rate")]),
    (1, "Low-confidence values per run", "Values that failed a check and wait for a person",
     "scores-numeric", "LINE_TIME_SERIES", [{"measure": "value", "agg": "avg"}], [], [score("low-confidence-values")]),
    (1, "Check failures by kind", "Re-reads on Opus, fuel-tax returns that don't add up, commodity model/keyword disagreements, analyst points without evidence",
     "scores-numeric", "VERTICAL_BAR", [{"measure": "value", "agg": "sum"}], [{"field": "name"}],
     [scores("escalations", "ifta-unreconciled", "commodity-disagreements", "analyst-unsupported-items")]),
    (1, "Answers served from cache", "Share of model calls answered from the response cache in a run",
     "scores-numeric", "NUMBER", [{"measure": "value", "agg": "avg"}], [], [score("cache-hit-rate")]),
    # 3. people
    (2, "Fields underwriters corrected", "Which values people fix most: the next thing to improve in extraction",
     "scores-categorical", "HORIZONTAL_BAR", [{"measure": "count", "agg": "count"}], [{"field": "stringValue"}],
     [score("corrected-field")]),
    (2, "Review actions", "Corrections, conflict decisions and rules re-runs",
     "scores-categorical", "PIE", [{"measure": "count", "agg": "count"}], [{"field": "stringValue"}],
     [score("review-action")]),
    (2, "Decisions", "Decline, refer, ready to quote after checks, ready to quote",
     "scores-categorical", "PIE", [{"measure": "count", "agg": "count"}], [{"field": "stringValue"}], [score("decision")]),
    (2, "Reviews that changed the decision", "Share of review actions after which the rules decided differently",
     "scores-boolean", "NUMBER", [{"measure": "value", "agg": "avg"}], [], [score("decision-changed")]),
]


def post(client, path, body):
    r = client.post(f"{BASE}/api/public/unstable/{path}", json=body)
    if r.status_code >= 300:
        sys.exit(f"{path}: HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def main():
    if not all(AUTH):
        sys.exit("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env")
    with httpx.Client(auth=AUTH, timeout=30) as client:
        dash = post(client, "dashboards", {
            "name": "Knight Intake: cost, trust and review",
            "description": "1. What does a submission cost? 2. Can we trust what the AI read? 3. Where do people step in?"})
        row_heights = [5, 5, 6]
        try:
            for i, (row, name, desc, view, chart, metrics, dims, filters) in enumerate(WIDGETS):
                widget = post(client, "dashboard-widgets", {"name": name, "description": desc, "view": view,
                                                            "chartType": chart, "metrics": metrics,
                                                            "dimensions": dims, "filters": filters})
                col = sum(1 for w in WIDGETS[:i] if w[0] == row)
                post(client, f"dashboards/{dash['id']}/placements", {
                    "type": "widget", "widgetId": widget["id"], "x": col * 3, "y": sum(row_heights[:row]),
                    "width": 3, "height": row_heights[row]})
        except SystemExit:
            client.delete(f"{BASE}/api/public/unstable/dashboards/{dash['id']}")   # no half-built dashboard left behind
            raise
        project = client.get(f"{BASE}/api/public/projects").json()["data"][0]["id"]
    print(f"{BASE}/project/{project}/dashboards/{dash['id']}")


if __name__ == "__main__":
    main()
