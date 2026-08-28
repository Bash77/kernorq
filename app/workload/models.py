"""
Workload Domain Model — Phase 1

Structured representation of externally supplied work (CSV rows) before it
enters the Kernorq execution system.

Boundaries
----------
This module is independent of the execution implementation
(app/orchestration/*). Translation to the execution system happens only in
app/workload/adapter.py via the existing plan-dict contract.

WorkloadStatus is a PLANNING-LAYER classification (what may be scheduled),
deliberately distinct from the runtime TaskStatus lifecycle (RUNNING,
VERIFYING, SUCCEEDED, ...). INVALID has no runtime counterpart: invalid
tasks never enter execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
_DEADLINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class WorkloadError(Exception):
    """Base exception for the workload subsystem."""


class WorkloadValidationError(WorkloadError):
    """Raised when one or more workload inputs fail validation.

    Aggregates every problem found (deterministic order) so callers see the
    complete correction list instead of failing row by row.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("Workload validation failed:\n" + "\n".join(f"- {e}" for e in self.errors))


class DependencyCycleError(WorkloadError):
    """Raised when workload dependencies contain a cycle."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("Dependency cycle detected: " + " -> ".join([*self.cycle, self.cycle[0]]))


class WorkloadStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"


@dataclass
class WorkloadTask:
    """A single unit of externally supplied work.

    Attributes:
        id: Stable unique identifier ([A-Za-z0-9_-], 1-64 chars).
        title: Short human-readable title.
        description: Optional longer description.
        priority: Integer 1 (highest) to 5 (lowest). Default 3.
        deadline: Optional ISO date (YYYY-MM-DD).
        dependencies: IDs of tasks that must complete first.
        task_type: Free-form category label (default "generic").
        status: Planning status; PENDING until a planner classifies it.
    """

    id: str
    title: str
    description: str = ""
    priority: int = 3
    deadline: date | None = None
    dependencies: list[str] = field(default_factory=list)
    task_type: str = "generic"
    status: WorkloadStatus = WorkloadStatus.PENDING

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _TASK_ID_RE.match(self.id):
            raise WorkloadValidationError(
                [f"task '{self.id}': id must match ^[a-zA-Z0-9_\\-]{{1,64}}$"]
            )
        if not isinstance(self.title, str) or not self.title.strip():
            raise WorkloadValidationError([f"task '{self.id}': title must be a non-empty string"])
        if not isinstance(self.description, str):
            raise WorkloadValidationError([f"task '{self.id}': description must be a string"])
        if not isinstance(self.priority, int) or isinstance(self.priority, bool) or not (1 <= self.priority <= 5):
            raise WorkloadValidationError(
                [f"task '{self.id}': priority must be an integer 1-5 (1=highest), got {self.priority!r}"]
            )
        if self.deadline is not None and not isinstance(self.deadline, date):
            raise WorkloadValidationError(
                [f"task '{self.id}': deadline must be a datetime.date or None"]
            )
        if not isinstance(self.dependencies, list) or not all(isinstance(d, str) for d in self.dependencies):
            raise WorkloadValidationError([f"task '{self.id}': dependencies must be a list of task ids"])
        if len(set(self.dependencies)) != len(self.dependencies):
            raise WorkloadValidationError([f"task '{self.id}': duplicate entries in dependencies"])
        if self.id in self.dependencies:
            raise WorkloadValidationError([f"task '{self.id}': task cannot depend on itself"])
        if not isinstance(self.task_type, str) or not self.task_type.strip():
            raise WorkloadValidationError([f"task '{self.id}': task_type must be a non-empty string"])
        if not isinstance(self.status, WorkloadStatus):
            raise WorkloadValidationError([f"task '{self.id}': status must be a WorkloadStatus"])


def parse_deadline(raw: str) -> date:
    """Parses a strict ISO deadline (YYYY-MM-DD); raises ValueError otherwise."""
    if not _DEADLINE_RE.match(raw):
        raise ValueError(f"deadline '{raw}' must use ISO format YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"deadline '{raw}' is not a valid calendar date: {exc}") from exc


def normalize_dependencies(raw: str) -> list[str]:
    """Normalizes a raw dependency field ('A; B' -> ['A','B']; '' -> [])."""
    if raw is None or not raw.strip():
        return []
    parts = [p.strip() for p in raw.split(";")]
    if any(p == "" for p in parts):
        raise ValueError(f"dependencies '{raw}' contains an empty entry (use ';' between ids)")
    return parts


__all__ = [
    "WorkloadError",
    "WorkloadValidationError",
    "DependencyCycleError",
    "WorkloadStatus",
    "WorkloadTask",
    "parse_deadline",
    "normalize_dependencies",
]
