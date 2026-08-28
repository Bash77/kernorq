# Kernorq — run the canonical golden demo workload
#
# Usage:
#   uv run python scripts/run_demo_workload.py            # in-memory store
#   uv run python scripts/run_demo_workload.py --store demo.db
#
# Loads demo/workloads/golden_demo.csv, executes it through the existing
# Phase 2 workload manager + orchestrator (workload scheduling policy,
# existing executor/verifier/recovery), and prints the actual result.
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory.sqlite_store import SQLiteExecutionStore  # noqa: E402
from app.workload.golden_demo import (  # noqa: E402
    PRIORITY_LABEL_MAP,
    load_golden_demo_tasks,
    run_golden_demo,
    workload_ui_summary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Kernorq golden demo workload")
    parser.add_argument("--csv", default=None, help="Optional alternative CSV path (default: canonical demo asset)")
    parser.add_argument("--store", default=None, help="Optional SQLite file for persistent execution state")
    args = parser.parse_args()

    tasks = load_golden_demo_tasks(args.csv)
    store = SQLiteExecutionStore(args.store) if args.store else None
    result = run_golden_demo(store=store)

    print(f"WORKLOAD  {len(tasks)} tasks")
    print(f"planned order: {result.planned_order}")
    summary = workload_ui_summary(result)
    print(f"counts: {summary['counts']}")
    print()
    print(f"{'#':>3}  {'id':<4} {'prio':<4} {'deadline':<10} {'status':<10} title")
    for view in summary["tasks"]:
        prio = str(view["priority"]) if view["priority"] is not None else "-"
        print(
            f"{view['selection_rank']:>3}  {view['task_id']:<4} {prio:<4} "
            f"{view['deadline'] or '-':<10} {view['status']:<10} {view['title']}  [{view['why']}]"
        )

    status = result.execution.status.value
    print(f"\nexecution {result.execution.execution_id}: {status}")
    return 0 if status == "COMPLETED" else 1


if __name__ == "__main__":
    labels = "/".join(PRIORITY_LABEL_MAP)
    raise SystemExit(main())
