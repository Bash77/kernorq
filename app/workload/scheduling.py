"""
Workload-aware Scheduling Policy — Phase 2

Ranks currently READY runtime tasks using the Phase 1 deterministic workload
ordering:

    priority ASC (1 = highest)
    deadline ASC (no deadline LAST)
    task_id ASC  (stable tie-break)

This is a pure ranking function over the tasks the orchestrator reports as
READY right now. It never mutates state, never bypasses readiness checks,
and holds only a lightweight {task_id: (priority, deadline)} map — the
runtime Task model is not extended with workload fields.

Unknown task ids (not from this workload) rank after known ones so a policy
attached to a mixed execution degrades deterministically instead of crashing.
"""

from __future__ import annotations

from datetime import date

from app.workload.models import WorkloadTask

_MAX_DATE = date.max
_DEFAULT_PRIORITY = 3


class WorkloadSchedulingPolicy:
    """Callable scheduling policy for ExecutionOrchestrator(scheduling_policy=...)."""

    def __init__(self, tasks: list[WorkloadTask]) -> None:
        self._meta: dict[str, tuple[int, date]] = {
            t.id: (t.priority, t.deadline or _MAX_DATE) for t in tasks
        }

    def _key(self, task_id: str) -> tuple[int, int, date, str]:
        priority, deadline = self._meta.get(task_id, (_DEFAULT_PRIORITY, _MAX_DATE))
        known = 0 if task_id in self._meta else 1
        return (known, priority, deadline, task_id)

    def __call__(self, ready_tasks: list) -> list[str]:
        return sorted((t.task_id for t in ready_tasks), key=self._key)


__all__ = ["WorkloadSchedulingPolicy"]
