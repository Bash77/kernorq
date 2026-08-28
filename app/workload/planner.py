"""
Workload Planner — Phase 1

Transforms List[WorkloadTask] into a deterministic WorkloadPlan.

Responsibilities (planning only — never executes anything):
- validate dependency references (missing -> INVALID entry)
- detect circular dependencies (DependencyCycleError, never a hang)
- classify every task READY / BLOCKED / INVALID
- produce a deterministic execution_order for all valid tasks

INVALID tasks are excluded from execution_order and reported explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.workload.models import DependencyCycleError, WorkloadStatus, WorkloadTask
from app.workload.prioritizer import build_execution_order


@dataclass
class WorkloadPlanEntry:
    task_id: str
    status: WorkloadStatus
    reason: str = ""


@dataclass
class WorkloadPlan:
    entries: dict[str, WorkloadPlanEntry] = field(default_factory=dict)
    ready_task_ids: list[str] = field(default_factory=list)
    blocked_task_ids: list[str] = field(default_factory=list)
    invalid_task_ids: list[str] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)

    def status_of(self, task_id: str) -> WorkloadStatus:
        return self.entries[task_id].status


def _find_cycle(tasks_by_id: dict[str, WorkloadTask]) -> list[str] | None:
    """DFS cycle detection over valid-reference edges only."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in tasks_by_id}

    def visit(node: str, stack: list[str]) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for dep in tasks_by_id[node].dependencies:
            if dep not in tasks_by_id:
                continue  # missing refs are INVALID, not cycles
            if color[dep] == GRAY:
                return stack[stack.index(dep):]
            if color[dep] == WHITE:
                found = visit(dep, stack)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for tid in sorted(tasks_by_id):
        if color[tid] == WHITE:
            found = visit(tid, [])
            if found:
                return found
    return None


def build_workload_plan(tasks: list[WorkloadTask]) -> WorkloadPlan:
    """Classifies tasks and computes the deterministic execution order.

    Raises:
        DependencyCycleError: if dependencies form a cycle.
    """
    tasks_by_id: dict[str, WorkloadTask] = {}
    duplicates: list[str] = []
    for t in tasks:
        if t.id in tasks_by_id and t.id not in duplicates:
            duplicates.append(t.id)
        tasks_by_id[t.id] = t
    if duplicates:
        raise ValueError(f"duplicate workload task ids: {', '.join(sorted(duplicates))}")

    cycle = _find_cycle(tasks_by_id)
    if cycle:
        raise DependencyCycleError(cycle)

    plan = WorkloadPlan()

    # Pass 1: missing references -> INVALID
    invalid_ids: set[str] = set()
    for tid, task in tasks_by_id.items():
        missing = [d for d in task.dependencies if d not in tasks_by_id]
        if missing:
            invalid_ids.add(tid)
            plan.entries[tid] = WorkloadPlanEntry(
                task_id=tid,
                status=WorkloadStatus.INVALID,
                reason=f"missing dependency id(s): {', '.join(sorted(missing))}",
            )

    # Pass 2: transitive dependents of INVALID are unschedulable too.
    changed = True
    while changed:
        changed = False
        for tid, task in tasks_by_id.items():
            if tid in invalid_ids or tid in plan.entries:
                continue
            bad_parents = [d for d in task.dependencies if d in invalid_ids]
            if bad_parents:
                invalid_ids.add(tid)
                plan.entries[tid] = WorkloadPlanEntry(
                    task_id=tid,
                    status=WorkloadStatus.INVALID,
                    reason=f"depends on unschedulable task(s): {', '.join(sorted(bad_parents))}",
                )
                changed = True

    # Pass 3: classification over schedulable tasks.
    # READY   = no dependencies -> schedulable immediately.
    # BLOCKED = waits on other schedulable tasks (they must execute first,
    #           mirroring runtime is_ready() which requires deps SUCCEEDED).
    schedulable = {tid for tid in tasks_by_id if tid not in invalid_ids}
    for tid in sorted(schedulable):
        task = tasks_by_id[tid]
        wait_deps = [d for d in task.dependencies if d in schedulable]
        if not wait_deps:
            plan.entries[tid] = WorkloadPlanEntry(
                task_id=tid, status=WorkloadStatus.READY, reason="no dependencies"
            )
        else:
            plan.entries[tid] = WorkloadPlanEntry(
                task_id=tid,
                status=WorkloadStatus.BLOCKED,
                reason=f"waiting on: {', '.join(sorted(wait_deps))}",
            )

    plan.invalid_task_ids = sorted(invalid_ids)
    plan.ready_task_ids = sorted(
        tid for tid, e in plan.entries.items() if e.status == WorkloadStatus.READY
    )
    plan.blocked_task_ids = sorted(
        tid for tid, e in plan.entries.items() if e.status == WorkloadStatus.BLOCKED
    )
    plan.execution_order = build_execution_order(tasks, schedulable) if schedulable else []

    # Sanity: every task got classified exactly once
    assert len(plan.entries) == len(tasks_by_id), "planner failed to classify all tasks"

    return plan


__all__ = ["WorkloadPlan", "WorkloadPlanEntry", "build_workload_plan"]
