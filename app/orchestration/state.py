from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


class InvalidStateTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""


class DependencyNotMetError(Exception):
    """Raised when a task cannot proceed because its dependencies are not satisfied."""


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class VerificationStatus(str, Enum):
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"
    UNKNOWN = "unknown"


class EventType(str, Enum):
    EXECUTION_CREATED = "EXECUTION_CREATED"
    PLAN_CREATED = "PLAN_CREATED"
    TASK_READY = "TASK_READY"
    TASK_STARTED = "TASK_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_SUCCEEDED = "VERIFICATION_SUCCEEDED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"


VALID_EXECUTION_TRANSITIONS: dict[ExecutionStatus, set[ExecutionStatus]] = {
    ExecutionStatus.PENDING: {ExecutionStatus.PLANNING, ExecutionStatus.CANCELLED},
    ExecutionStatus.PLANNING: {ExecutionStatus.EXECUTING, ExecutionStatus.CANCELLED},
    ExecutionStatus.EXECUTING: {ExecutionStatus.VERIFYING, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED},
    ExecutionStatus.VERIFYING: {ExecutionStatus.COMPLETED, ExecutionStatus.RECOVERING, ExecutionStatus.FAILED},
    ExecutionStatus.RECOVERING: {ExecutionStatus.EXECUTING, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED},
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.FAILED: set(),
    ExecutionStatus.CANCELLED: set(),
}

VALID_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.VERIFYING: {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: {TaskStatus.READY},  # Retries must reset to READY before RUNNING
    TaskStatus.CANCELLED: set(),
}



def utc_now() -> datetime:
    """Returns the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


@dataclass
class VerificationResult:
    status: VerificationStatus
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)

    def is_success(self) -> bool:
        return self.status == VerificationStatus.VERIFIED_SUCCESS

    def is_failure(self) -> bool:
        return self.status == VerificationStatus.VERIFIED_FAILURE

    def is_unknown(self) -> bool:
        return self.status == VerificationStatus.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "evidence": self.evidence,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class Checkpoint:
    checkpoint_id: str
    execution_id: str
    task_id: str | None
    state_snapshot: dict[str, Any]
    reason: str
    timestamp: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "state_snapshot": self.state_snapshot,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ExecutionEvent:
    event_id: str
    execution_id: str
    event_type: EventType
    timestamp: datetime = field(default_factory=utc_now)
    task_id: str | None = None
    actor: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "execution_id": self.execution_id,
            "event_type": self.event_type.value,
            "task_id": self.task_id,
            "actor": self.actor,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class Task:
    task_id: str
    execution_id: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = field(default_factory=list)
    attempt_count: int = 0
    max_attempts: int = 3
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    result: dict[str, Any] | None = None
    verification: VerificationResult | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def transition_to(self, new_status: TaskStatus) -> None:
        """Transitions the task to a new status following valid state rules.

        Raises:
            InvalidStateTransitionError: If the transition is not allowed.
        """
        allowed = VALID_TASK_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid task transition from {self.status.value} to {new_status.value} "
                f"for task {self.task_id}"
            )
        self.status = new_status
        if new_status == TaskStatus.RUNNING and self.started_at is None:
            self.started_at = utc_now()
        elif new_status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            self.completed_at = utc_now()

    def is_ready(self, all_tasks: dict[str, Task]) -> bool:
        """Checks whether all dependencies for this task have SUCCEEDED."""
        for dep_id in self.dependencies:
            dep_task = all_tasks.get(dep_id)
            if dep_task is None or dep_task.status != TaskStatus.SUCCEEDED:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "dependencies": list(self.dependencies),
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "operation_id": self.operation_id,
            "result": self.result,
            "verification": self.verification.to_dict() if self.verification else None,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class Execution:
    execution_id: str
    objective: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    tasks: dict[str, Task] = field(default_factory=dict)
    current_task_id: str | None = None
    attempt_count: int = 0
    checkpoints: list[Checkpoint] = field(default_factory=list)
    verification_results: list[VerificationResult] = field(default_factory=list)
    last_error: dict[str, Any] | None = None
    recovery_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def transition_to(self, new_status: ExecutionStatus) -> None:
        """Transitions execution to a new status following valid state rules.

        Raises:
            InvalidStateTransitionError: If the transition is not allowed.
        """
        allowed = VALID_EXECUTION_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid execution transition from {self.status.value} to {new_status.value} "
                f"for execution {self.execution_id}"
            )
        self.status = new_status
        self.updated_at = utc_now()

    def add_task(self, task: Task) -> None:
        """Adds a task to the execution."""
        task.execution_id = self.execution_id
        self.tasks[task.task_id] = task
        self.updated_at = utc_now()

    def get_ready_tasks(self) -> list[Task]:
        """Returns all tasks that are currently PENDING or BLOCKED but whose dependencies have SUCCEEDED."""
        ready: list[Task] = []
        for task in self.tasks.values():
            if task.status in {TaskStatus.PENDING, TaskStatus.BLOCKED}:
                if task.is_ready(self.tasks):
                    ready.append(task)
        return ready

    def update_task_statuses(self) -> None:
        """Deterministically evaluates and updates task statuses based on dependency states."""
        for task in self.tasks.values():
            if task.status == TaskStatus.PENDING:
                if task.is_ready(self.tasks):
                    task.transition_to(TaskStatus.READY)
                elif any(
                    self.tasks.get(dep_id) and self.tasks[dep_id].status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
                    for dep_id in task.dependencies
                ):
                    task.transition_to(TaskStatus.BLOCKED)
            elif task.status == TaskStatus.BLOCKED:
                if task.is_ready(self.tasks):
                    task.transition_to(TaskStatus.READY)
        self.updated_at = utc_now()

    def snapshot(self) -> dict[str, Any]:
        """Returns an immutable dictionary snapshot of the execution state."""
        return {
            "execution_id": self.execution_id,
            "objective": self.objective,
            "status": self.status.value,
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "current_task_id": self.current_task_id,
            "attempt_count": self.attempt_count,
            "checkpoints": [c.to_dict() for c in self.checkpoints],
            "verification_results": [v.to_dict() for v in self.verification_results],
            "last_error": self.last_error,
            "recovery_history": list(self.recovery_history),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()


__all__ = [
    "Execution",
    "Task",
    "Checkpoint",
    "VerificationResult",
    "ExecutionEvent",
    "ExecutionStatus",
    "TaskStatus",
    "VerificationStatus",
    "EventType",
    "InvalidStateTransitionError",
    "DependencyNotMetError",
    "VALID_EXECUTION_TRANSITIONS",
    "VALID_TASK_TRANSITIONS",
    "utc_now",
]
