"""
Workload Prioritization — Phase 1

Deterministic, LLM-free ordering of schedulable workload tasks.

Ordering rule (applied greedily to the dependency-ready set):
    1. dependency readiness   (a task is only ordered after its deps)
    2. priority               (ascending integer: 1 = highest first)
    3. deadline               (earlier first; no-deadline last)
    4. task id                (stable lexicographic tie-breaker)

The same input always produces the same output.
"""

from __future__ import annotations

from datetime import date

from app.workload.models import WorkloadTask

_MAX_DATE = date.max


def _sort_key(task: WorkloadTask) -> tuple[int, date, str]:
    return (task.priority, task.deadline or _MAX_DATE, task.id)


def prioritize_ready(ready: list[WorkloadTask]) -> list[WorkloadTask]:
    """Returns the ready tasks in deterministic execution order."""
    return sorted(ready, key=_sort_key)


def build_execution_order(
    tasks: list[WorkloadTask],
    valid_ids: set[str],
) -> list[str]:
    """Produces a deterministic topological execution order for valid tasks.

    Greedy Kahn-style selection: at each step, among all tasks whose
    dependencies are already ordered (and thus satisfied), pick the one with
    the best (priority, deadline, id) key. This guarantees dependencies come
    before dependents while keeping priority/deadline preference inside each
    readiness wave.

    Raises:
        RuntimeError: if a cycle prevents completion (planner detects cycles
            earlier; this is a safety net).
    """
    by_id = {t.id: t for t in tasks}
    remaining = {tid for tid in by_id if tid in valid_ids}
    placed: set[str] = set()
    order: list[str] = []

    while remaining:
        # Only consider edges to other valid tasks; invalid ids were excluded
        # by the planner and can never satisfy/block anything here.
        ready_now = [
            by_id[tid]
            for tid in remaining
            if all(dep not in valid_ids or dep in placed for dep in by_id[tid].dependencies)
        ]
        if not ready_now:
            raise RuntimeError(
                f"dependency deadlock among: {sorted(remaining)} (cycle not caught earlier?)"
            )
        choice = min(ready_now, key=_sort_key)
        order.append(choice.id)
        placed.add(choice.id)
        remaining.discard(choice.id)

    return order


__all__ = ["prioritize_ready", "build_execution_order"]
