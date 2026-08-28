from __future__ import annotations

import copy
from typing import Protocol
import uuid

from app.orchestration.state import (
    Checkpoint,
    Execution,
    ExecutionEvent,
    utc_now,
)


class ExecutionNotFoundError(Exception):
    """Raised when an execution cannot be found in the store."""


class ExecutionStore(Protocol):
    """Abstract interface for execution persistence."""

    def create_execution(self, execution: Execution) -> Execution: ...
    def get_execution(self, execution_id: str) -> Execution: ...
    def update_execution(self, execution: Execution) -> Execution: ...
    def append_event(self, event: ExecutionEvent) -> None: ...
    def get_events(self, execution_id: str) -> list[ExecutionEvent]: ...
    def create_checkpoint(
        self, execution_id: str, reason: str, task_id: str | None = None
    ) -> Checkpoint: ...
    def get_checkpoints(self, execution_id: str) -> list[Checkpoint]: ...
    def list_executions(self) -> list[Execution]: ...


class InMemoryExecutionStore:
    """In-memory execution store implementation with isolated storage and retrieval."""

    def __init__(self) -> None:
        self._executions: dict[str, Execution] = {}
        self._events: dict[str, list[ExecutionEvent]] = {}
        self._checkpoints: dict[str, list[Checkpoint]] = {}

    def create_execution(self, execution: Execution) -> Execution:
        """Stores a new execution using deep copy isolation.

        Raises:
            ValueError: If an execution with the same ID already exists.
        """
        if execution.execution_id in self._executions:
            raise ValueError(f"Execution {execution.execution_id} already exists")

        stored_execution = copy.deepcopy(execution)
        self._executions[execution.execution_id] = stored_execution
        self._events[execution.execution_id] = []
        self._checkpoints[execution.execution_id] = []
        return copy.deepcopy(stored_execution)

    def get_execution(self, execution_id: str) -> Execution:
        """Retrieves an isolated copy of an execution by ID.

        Raises:
            ExecutionNotFoundError: If execution does not exist.
        """
        execution = self._executions.get(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")
        return copy.deepcopy(execution)

    def update_execution(self, execution: Execution) -> Execution:
        """Updates an existing execution using deep copy isolation.

        Raises:
            ExecutionNotFoundError: If execution does not exist.
        """
        if execution.execution_id not in self._executions:
            raise ExecutionNotFoundError(f"Execution {execution.execution_id} not found")
        updated = copy.deepcopy(execution)
        updated.updated_at = utc_now()
        self._executions[execution.execution_id] = updated
        return copy.deepcopy(updated)

    def append_event(self, event: ExecutionEvent) -> None:
        """Appends an execution audit event in an append-only manner using isolated copies."""
        if event.execution_id not in self._events:
            self._events[event.execution_id] = []
        self._events[event.execution_id].append(copy.deepcopy(event))

    def get_events(self, execution_id: str) -> list[ExecutionEvent]:
        """Returns isolated copies of all events recorded for the given execution ID."""
        return [copy.deepcopy(evt) for evt in self._events.get(execution_id, [])]

    def create_checkpoint(
        self, execution_id: str, reason: str, task_id: str | None = None
    ) -> Checkpoint:
        """Creates and stores an immutable state snapshot checkpoint.

        The snapshot captures tasks/execution state but deliberately EXCLUDES
        the checkpoints list itself: nesting historical checkpoints (each with
        their own snapshots) inside every new snapshot makes storage grow
        exponentially with execution length. Consumers read checkpoints via
        get_checkpoints(); no reader consumes nested copies.

        Raises:
            ExecutionNotFoundError: If execution does not exist.
        """
        if execution_id not in self._executions:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")

        # Snapshot the stored execution state (pruned — see docstring)
        execution_in_store = self._executions[execution_id]
        snapshot = execution_in_store.snapshot()
        snapshot.pop("checkpoints", None)
        checkpoint = Checkpoint(
            checkpoint_id=f"chk_{uuid.uuid4().hex[:12]}",
            execution_id=execution_id,
            task_id=task_id,
            state_snapshot=snapshot,
            reason=reason,
            timestamp=utc_now(),
        )
        # Internal lists are only exposed via get_* which return isolated copies.
        stored_for_execution = copy.deepcopy(checkpoint)
        stored_for_execution.state_snapshot = dict(snapshot)
        execution_in_store.checkpoints.append(stored_for_execution)
        self._checkpoints[execution_id].append(checkpoint)
        return copy.deepcopy(checkpoint)

    def get_checkpoints(self, execution_id: str) -> list[Checkpoint]:
        """Returns isolated copies of all checkpoints for the given execution ID."""
        return [copy.deepcopy(chk) for chk in self._checkpoints.get(execution_id, [])]

    def list_executions(self) -> list[Execution]:
        """Returns isolated copies of all executions."""
        return [copy.deepcopy(e) for e in self._executions.values()]


__all__ = [
    "ExecutionStore",
    "InMemoryExecutionStore",
    "ExecutionNotFoundError",
]

