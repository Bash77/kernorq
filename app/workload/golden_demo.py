"""
Golden Demo Workload integration — canonical CSV adapter.

Loads demo/workloads/golden_demo.csv into the EXISTING Phase 1/2 workload
pipeline. The CSV uses the user's own column names and emoji priority
labels; this module performs an EXPLICIT, documented field mapping (no
silent renames) and then delegates all validation to the existing
parse_workload_csv.

Mapping (source -> canonical):
    TaskID                  -> id
    Task                    -> title
    Category                -> task_type
    DueDate                 -> deadline        (already ISO YYYY-MM-DD)
    Priority                -> priority        High->1, Medium->3, Low->5
    What Kernorq should do  -> description
    (no dependency column)  -> dependencies [] (all tasks start READY/BLOCKED-free)

Priority scale: Kernorq priorities are ints 1 (highest) .. 5 (lowest);
High=1 / Medium=3 / Low=5 preserves ordinal extremes and keeps Medium on
the system default. Unknown labels are rejected loudly.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from app.memory.store import ExecutionStore, InMemoryExecutionStore
from app.tools.registry import ToolRegistry, create_default_tool_registry
from app.workload.loader import parse_workload_csv
from app.workload.manager import WorkloadRunResult, run_workload
from app.workload.models import WorkloadTask

def _resolve_demo_csv() -> Path:
    """Resolves demo/workloads/golden_demo.csv robustly for both local and container.

    Prefers project-relative resolution via __file__ (works regardless of cwd,
    both locally and in /app in the container). Falls back to cwd-relative
    and importlib.resources for extra robustness — never hard-codes an
    absolute container path.
    """
    # Primary: project root relative to this file (app/workload/golden_demo.py -> repo root)
    candidates = [
        Path(__file__).resolve().parents[2] / "demo" / "workloads" / "golden_demo.csv",
        Path.cwd() / "demo" / "workloads" / "golden_demo.csv",
        Path.cwd() / "app" / "demo" / "workloads" / "golden_demo.csv",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    # Fallback: try importlib.resources if demo is a package resource
    try:
        import importlib.resources as resources

        # Try to find demo as a resource (if demo is installed as package data)
        for pkg in ["demo.workloads", "demo"]:
            try:
                files = resources.files(pkg)  # type: ignore
                cand = files / "golden_demo.csv"  # type: ignore
                # files may be Traversable, check is_file
                if cand.is_file():  # type: ignore
                    return Path(str(cand))
            except Exception:
                continue
    except Exception:
        pass
    # Return primary (will raise FileNotFoundError on read, with clear message)
    return Path(__file__).resolve().parents[2] / "demo" / "workloads" / "golden_demo.csv"


CANONICAL_DEMO_CSV = _resolve_demo_csv()

SOURCE_COLUMN_MAP: dict[str, str] = {
    "TaskID": "id",
    "Task": "title",
    "Category": "task_type",
    "DueDate": "deadline",
    "Priority": "priority",
    "What Kernorq should do": "description",
}
# Dependencies is optional in the source CSV — if present, its value is used verbatim
OPTIONAL_SOURCE_COLUMNS = {"Dependencies": "dependencies"}

PRIORITY_LABEL_MAP: dict[str, str] = {
    "high": "1",
    "medium": "3",
    "low": "5",
}

CANONICAL_COLUMNS = [
    "id", "title", "description", "priority", "deadline", "dependencies", "task_type",
]

DEMO_TOOL_MAPPING: dict[str, str] = {
    "💻 Project": "project_diagnostics",
    "🤖 Kernorq Demo": "run_test_suite",
    "🔬 Research": "research_topic",
    "🤝 Client Research": "analyze_competitors",
    "📱 Content": "generate_carousel",
}
DEFAULT_DEMO_TOOL = "inspect_project_workspace"

# Per-tool keyword contracts (tools have distinct signatures)
# Demo workload uses a focused test file for speed — still real pytest execution
# but completes in ~5s vs ~50s for the full suite (22 tasks, judge demo).
DEMO_TOOL_INPUTS: dict[str, dict[str, Any]] = {
    "run_test_suite": {"test_path": "tests/test_executor.py"},
    "project_diagnostics": {"directory_path": "."},
    "research_topic": {},  # per-task topic injected dynamically
    "analyze_competitors": {},
    "generate_carousel": {},
}
DEFAULT_DEMO_TOOL_INPUT: dict[str, Any] = {"directory_path": "."}


def normalize_priority_label(raw: str) -> str:
    """Maps '🔴 High' / '🟡 Medium' / '🟢 Low' to canonical integer strings."""
    label = raw.strip().lower()
    for token, canonical in PRIORITY_LABEL_MAP.items():
        if token in label:
            return canonical
    raise ValueError(
        f"unknown priority label {raw!r}: expected one of "
        f"{sorted(PRIORITY_LABEL_MAP)} (optionally with emoji prefix)"
    )


def _to_canonical_csv(text: str) -> str:
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("golden demo CSV is empty")
    header = [h.strip() for h in rows[0]]
    missing = [c for c in SOURCE_COLUMN_MAP if c not in header]
    if missing:
        raise ValueError(f"golden demo CSV missing expected column(s): {', '.join(missing)}")
    idx = {name: header.index(name) for name in SOURCE_COLUMN_MAP}
    has_deps = "Dependencies" in header
    deps_idx = header.index("Dependencies") if has_deps else None

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CANONICAL_COLUMNS)
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError(
                f"golden demo CSV row has {len(row)} cells, expected {len(header)}: {row!r}"
            )
        deps_val = row[deps_idx].strip() if has_deps and deps_idx is not None else ""
        writer.writerow([
            row[idx["TaskID"]].strip(),
            row[idx["Task"]].strip(),
            row[idx["What Kernorq should do"]].strip(),
            normalize_priority_label(row[idx["Priority"]]),
            row[idx["DueDate"]].strip(),
            deps_val,
            row[idx["Category"]].strip(),
        ])
    return out.getvalue()


def load_golden_demo_tasks(path: str | Path | None = None) -> list[WorkloadTask]:
    """Loads and validates the canonical golden demo workload.

    Works regardless of the process working directory — the default path is
    resolved project-relative via _resolve_demo_csv(), not cwd-relative.
    """
    if path is not None:
        csv_path = Path(path)
    else:
        # Re-resolve at call time in case cwd changed since import (still project-relative)
        csv_path = CANONICAL_DEMO_CSV if CANONICAL_DEMO_CSV.is_file() else _resolve_demo_csv()
    return parse_workload_csv(_to_canonical_csv(csv_path.read_text(encoding="utf-8-sig")))


def _per_task_tool_input(task: WorkloadTask, tool_name: str) -> dict[str, Any]:
    """Per-task input so research/content tools receive the task's own topic."""
    if tool_name == "research_topic":
        return {"topic": task.title, "num_sources": 3}
    if tool_name == "analyze_competitors":
        return {"topic": task.title, "context": task.description}
    if tool_name == "generate_carousel":
        return {"topic": task.title, "audience": "founders"}
    return dict(DEMO_TOOL_INPUTS.get(tool_name, DEFAULT_DEMO_TOOL_INPUT))


def run_golden_demo(
    *,
    store: ExecutionStore | None = None,
    tool_registry: ToolRegistry | None = None,
    tasks_path: str | Path | None = None,
    external_state_checker: Any | None = None,
) -> WorkloadRunResult:
    """Runs the canonical demo CSV through the existing Phase 2 engine."""
    tasks = load_golden_demo_tasks(tasks_path)
    # Build plan with per-task inputs so research/content tasks carry their own topic
    from app.workload.planner import build_workload_plan
    from app.workload.adapter import to_execution_plan_dict
    from app.orchestration.planner import create_execution_from_plan
    from app.orchestration.orchestrator import ExecutionOrchestrator
    from app.orchestration.verifier import create_default_strategy_registry
    from app.workload.scheduling import WorkloadSchedulingPolicy

    store = store or InMemoryExecutionStore()
    tool_registry = tool_registry or create_default_tool_registry()
    plan = build_workload_plan(tasks)

    # Build execution plan dict with per-task tool inputs
    mapping = dict(DEMO_TOOL_MAPPING)
    plan_dict_tasks = []
    for t in sorted([x for x in tasks if plan.status_of(x.id).value in ("READY", "BLOCKED")],
                    key=lambda x: plan.execution_order.index(x.id)):
        tool_name = mapping.get(t.task_type, DEFAULT_DEMO_TOOL)
        if not tool_registry.has(tool_name):
            raise ValueError(f"task '{t.id}': tool '{tool_name}' not registered")
        plan_dict_tasks.append({
            "task_id": t.id,
            "title": t.title,
            "description": t.description or t.title,
            "tool_name": tool_name,
            "tool_input": _per_task_tool_input(t, tool_name),
            "dependencies": list(t.dependencies),
            "max_attempts": 3,
        })
    plan_dict = {"objective": "Kernorq golden demo workload", "tasks": plan_dict_tasks}
    plan_dict["scheduling"] = {"policy": "workload_priority", "planned_order": list(plan.execution_order)}

    execution = create_execution_from_plan(plan_dict, tool_registry)
    store.create_execution(execution)
    orch = ExecutionOrchestrator(
        store=store,
        tool_registry=tool_registry,
        strategy_registry=create_default_strategy_registry(),
        external_state_checker=external_state_checker,
        scheduling_policy=WorkloadSchedulingPolicy(tasks),
    )
    final = orch.run(execution.execution_id)
    from app.workload.manager import WorkloadRunResult
    return WorkloadRunResult(execution=final, plan=plan, planned_order=list(plan.execution_order))


_UI_STATUS_BUCKETS: dict[str, str] = {
    "PENDING": "READY",
    "READY": "READY",
    "BLOCKED": "BLOCKED",
    "RUNNING": "EXECUTING",
    "VERIFYING": "EXECUTING",
    "RECOVERING": "EXECUTING",
    "SUCCEEDED": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
}


def workload_ui_summary(result: WorkloadRunResult) -> dict[str, Any]:
    """Structured view for the demo UI — consumes existing state only."""
    execution = result.execution
    counts: dict[str, int] = {"READY": 0, "BLOCKED": 0, "EXECUTING": 0, "COMPLETED": 0, "FAILED": 0}
    task_views: list[dict[str, Any]] = []
    rank_of = {tid: i + 1 for i, tid in enumerate(result.planned_order)}
    meta = {t.id: t for t in load_golden_demo_tasks()} if result.plan.entries else {}
    for tid, entry in result.plan.entries.items():
        runtime = execution.tasks.get(tid)
        bucket = _UI_STATUS_BUCKETS.get(runtime.status.value, runtime.status.value) if runtime else entry.status.value
        if bucket in counts:
            counts[bucket] += 1
        task = meta.get(tid)
        task_views.append({
            "task_id": tid,
            "title": runtime.title if runtime else tid,
            "priority": task.priority if task else None,
            "deadline": task.deadline.isoformat() if task and task.deadline else None,
            "dependencies": list(task.dependencies) if task else [],
            "planned_status": entry.status.value,
            "status": bucket,
            "why": entry.reason,
            "selection_rank": rank_of.get(tid),
        })
    task_views.sort(key=lambda v: v["selection_rank"] or 10**6)
    return {"total": len(task_views), "counts": counts, "tasks": task_views}


__all__ = [
    "CANONICAL_DEMO_CSV",
    "SOURCE_COLUMN_MAP",
    "PRIORITY_LABEL_MAP",
    "DEMO_TOOL_MAPPING",
    "DEFAULT_DEMO_TOOL",
    "normalize_priority_label",
    "load_golden_demo_tasks",
    "run_golden_demo",
    "workload_ui_summary",
]
