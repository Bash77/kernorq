"""
SQLite Execution Store — Phase 2.8a/b

Persistent store matching InMemoryExecutionStore contract.
Uses stdlib sqlite3; no additional dependencies.

Preserves:
  - execution_id, objective, status, attempt_count, current_task_id
  - task.operation_id, attempt_count, status, result, verification, error, dependencies
  - events, checkpoints, recovery_history, verification_results, last_error
  - timestamps as ISO-8601 UTC

Reconstructed Execution is behaviorally equivalent to uninterrupted one.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.orchestration.state import (
    Checkpoint,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    EventType,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
    utc_now,
)
from app.memory.store import ExecutionNotFoundError


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _serialize_verification(v: VerificationResult | None) -> str | None:
    if v is None:
        return None
    return json.dumps(v.to_dict())


def _deserialize_verification(s: str | None) -> VerificationResult | None:
    if s is None or s == "null":
        return None
    try:
        d = json.loads(s)
        if d is None:
            return None
        return VerificationResult(
            status=VerificationStatus(d["status"]),
            message=d["message"],
            evidence=d.get("evidence", {}),
            timestamp=_parse_dt(d.get("timestamp")) or utc_now(),
        )
    except Exception:
        return None


class SQLiteExecutionStore:
    """SQLite-backed execution store. Implements ExecutionStore Protocol."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        # check_same_thread False for pytest parallel? Keep True default
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # Enable WAL for better concurrency
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                current_task_id TEXT,
                attempt_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT,
                recovery_history TEXT,
                verification_results TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                dependencies TEXT,
                attempt_count INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                tool_name TEXT,
                tool_input TEXT,
                operation_id TEXT NOT NULL,
                result TEXT,
                verification TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                PRIMARY KEY (task_id, execution_id),
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                task_id TEXT,
                actor TEXT,
                metadata TEXT,
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                task_id TEXT,
                state_snapshot TEXT,
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_exec ON tasks(execution_id);
            CREATE INDEX IF NOT EXISTS idx_events_exec ON events(execution_id);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_exec ON checkpoints(execution_id);
            """
        )

    # ------------------------------------------------------------------
    # Helpers — serialize Execution/Task
    # ------------------------------------------------------------------

    def _execution_to_row(self, execution: Execution) -> dict[str, Any]:
        return {
            "execution_id": execution.execution_id,
            "objective": execution.objective,
            "status": execution.status.value,
            "current_task_id": execution.current_task_id,
            "attempt_count": execution.attempt_count,
            "created_at": execution.created_at.isoformat(),
            "updated_at": execution.updated_at.isoformat(),
            "last_error": json.dumps(execution.last_error) if execution.last_error is not None else None,
            "recovery_history": json.dumps(execution.recovery_history),
            "verification_results": json.dumps([v.to_dict() for v in execution.verification_results]),
        }

    def _row_to_execution(self, row: sqlite3.Row) -> Execution:
        execution = Execution(
            execution_id=row["execution_id"],
            objective=row["objective"],
            status=ExecutionStatus(row["status"]),
            current_task_id=row["current_task_id"],
            attempt_count=row["attempt_count"],
            last_error=json.loads(row["last_error"]) if row["last_error"] else None,
            recovery_history=json.loads(row["recovery_history"]) if row["recovery_history"] else [],
            created_at=_parse_dt(row["created_at"]) or utc_now(),
            updated_at=_parse_dt(row["updated_at"]) or utc_now(),
        )
        # verification_results
        if row["verification_results"]:
            try:
                vlist = json.loads(row["verification_results"])
                for vd in vlist:
                    execution.verification_results.append(
                        VerificationResult(
                            status=VerificationStatus(vd["status"]),
                            message=vd["message"],
                            evidence=vd.get("evidence", {}),
                            timestamp=_parse_dt(vd.get("timestamp")) or utc_now(),
                        )
                    )
            except Exception:
                pass
        return execution

    def _task_to_row(self, task: Task, execution_id: str) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "execution_id": execution_id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "dependencies": json.dumps(task.dependencies),
            "attempt_count": task.attempt_count,
            "max_attempts": task.max_attempts,
            "tool_name": task.tool_name,
            "tool_input": json.dumps(task.tool_input),
            "operation_id": task.operation_id,
            "result": json.dumps(task.result) if task.result is not None else None,
            "verification": _serialize_verification(task.verification),
            "error": json.dumps(task.error) if task.error is not None else None,
            "created_at": task.created_at.isoformat(),
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        task = Task(
            task_id=row["task_id"],
            execution_id=row["execution_id"],
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            dependencies=json.loads(row["dependencies"]) if row["dependencies"] else [],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            tool_name=row["tool_name"],
            tool_input=json.loads(row["tool_input"]) if row["tool_input"] else {},
            operation_id=row["operation_id"],
            result=json.loads(row["result"]) if row["result"] else None,
            verification=_deserialize_verification(row["verification"]),
            error=json.loads(row["error"]) if row["error"] else None,
            created_at=_parse_dt(row["created_at"]) or utc_now(),
            started_at=_parse_dt(row["started_at"]),
            completed_at=_parse_dt(row["completed_at"]),
        )
        return task

    # ------------------------------------------------------------------
    # ExecutionStore Protocol implementation
    # ------------------------------------------------------------------

    def create_execution(self, execution: Execution) -> Execution:
        # Check exists
        cur = self._conn.execute("SELECT 1 FROM executions WHERE execution_id=?", (execution.execution_id,))
        if cur.fetchone() is not None:
            raise ValueError(f"Execution {execution.execution_id} already exists")
        row = self._execution_to_row(copy.deepcopy(execution))
        self._conn.execute(
            """
            INSERT INTO executions (execution_id, objective, status, current_task_id, attempt_count, created_at, updated_at, last_error, recovery_history, verification_results)
            VALUES (:execution_id, :objective, :status, :current_task_id, :attempt_count, :created_at, :updated_at, :last_error, :recovery_history, :verification_results)
            """,
            row,
        )
        # Insert tasks
        for task in execution.tasks.values():
            trow = self._task_to_row(task, execution.execution_id)
            self._conn.execute(
                """
                INSERT INTO tasks (task_id, execution_id, title, description, status, dependencies, attempt_count, max_attempts, tool_name, tool_input, operation_id, result, verification, error, created_at, started_at, completed_at)
                VALUES (:task_id, :execution_id, :title, :description, :status, :dependencies, :attempt_count, :max_attempts, :tool_name, :tool_input, :operation_id, :result, :verification, :error, :created_at, :started_at, :completed_at)
                """,
                trow,
            )
        self._conn.commit()
        return self.get_execution(execution.execution_id)

    def get_execution(self, execution_id: str) -> Execution:
        cur = self._conn.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        row = cur.fetchone()
        if row is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")
        execution = self._row_to_execution(row)
        # Load tasks
        cur = self._conn.execute("SELECT * FROM tasks WHERE execution_id=?", (execution_id,))
        for trow in cur.fetchall():
            task = self._row_to_task(trow)
            execution.tasks[task.task_id] = task
        # Load checkpoints into execution.checkpoints (for snapshot compatibility)
        cur = self._conn.execute("SELECT * FROM checkpoints WHERE execution_id=? ORDER BY timestamp", (execution_id,))
        execution.checkpoints = []
        for crow in cur.fetchall():
            try:
                snapshot = json.loads(crow["state_snapshot"]) if crow["state_snapshot"] else {}
            except Exception:
                snapshot = {}
            execution.checkpoints.append(
                Checkpoint(
                    checkpoint_id=crow["checkpoint_id"],
                    execution_id=crow["execution_id"],
                    task_id=crow["task_id"],
                    state_snapshot=snapshot,
                    reason=crow["reason"],
                    timestamp=_parse_dt(crow["timestamp"]) or utc_now(),
                )
            )
        return copy.deepcopy(execution)

    def update_execution(self, execution: Execution) -> Execution:
        cur = self._conn.execute("SELECT 1 FROM executions WHERE execution_id=?", (execution.execution_id,))
        if cur.fetchone() is None:
            raise ExecutionNotFoundError(f"Execution {execution.execution_id} not found")
        # Update updated_at deterministically
        execution.updated_at = utc_now()
        row = self._execution_to_row(copy.deepcopy(execution))
        self._conn.execute(
            """
            UPDATE executions SET objective=:objective, status=:status, current_task_id=:current_task_id, attempt_count=:attempt_count, updated_at=:updated_at, last_error=:last_error, recovery_history=:recovery_history, verification_results=:verification_results
            WHERE execution_id=:execution_id
            """,
            row,
        )
        # Replace tasks: delete then insert
        self._conn.execute("DELETE FROM tasks WHERE execution_id=?", (execution.execution_id,))
        for task in execution.tasks.values():
            trow = self._task_to_row(task, execution.execution_id)
            self._conn.execute(
                """
                INSERT INTO tasks (task_id, execution_id, title, description, status, dependencies, attempt_count, max_attempts, tool_name, tool_input, operation_id, result, verification, error, created_at, started_at, completed_at)
                VALUES (:task_id, :execution_id, :title, :description, :status, :dependencies, :attempt_count, :max_attempts, :tool_name, :tool_input, :operation_id, :result, :verification, :error, :created_at, :started_at, :completed_at)
                """,
                trow,
            )
        self._conn.commit()
        return self.get_execution(execution.execution_id)

    def append_event(self, event: ExecutionEvent) -> None:
        # Ensure execution exists
        cur = self._conn.execute("SELECT 1 FROM executions WHERE execution_id=?", (event.execution_id,))
        if cur.fetchone() is None:
            # Auto-create events bucket even if execution not yet? Mirror InMemory which auto-creates list
            # But for strict, we ensure execution exists; if not, still insert (no FK would fail)
            # We rely on FK, so check
            raise ExecutionNotFoundError(f"Execution {event.execution_id} not found for event")
        self._conn.execute(
            """
            INSERT OR IGNORE INTO events (event_id, execution_id, event_type, timestamp, task_id, actor, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.execution_id,
                event.event_type.value,
                event.timestamp.isoformat(),
                event.task_id,
                event.actor,
                json.dumps(event.metadata),
            ),
        )
        self._conn.commit()

    def get_events(self, execution_id: str) -> list[ExecutionEvent]:
        cur = self._conn.execute("SELECT * FROM events WHERE execution_id=? ORDER BY timestamp", (execution_id,))
        events: list[ExecutionEvent] = []
        for row in cur.fetchall():
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except Exception:
                metadata = {}
            events.append(
                ExecutionEvent(
                    event_id=row["event_id"],
                    execution_id=row["execution_id"],
                    event_type=EventType(row["event_type"]),
                    timestamp=_parse_dt(row["timestamp"]) or utc_now(),
                    task_id=row["task_id"],
                    actor=row["actor"],
                    metadata=metadata,
                )
            )
        # Return deep copies to match InMemory isolation
        return [copy.deepcopy(e) for e in events]

    def create_checkpoint(self, execution_id: str, reason: str, task_id: str | None = None) -> Checkpoint:
        cur = self._conn.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        row = cur.fetchone()
        if row is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} not found")
        # Build snapshot from current stored execution state
        execution = self.get_execution(execution_id)
        snapshot = copy.deepcopy(execution.snapshot())
        checkpoint = Checkpoint(
            checkpoint_id=f"chk_{uuid.uuid4().hex[:12]}",
            execution_id=execution_id,
            task_id=task_id,
            state_snapshot=snapshot,
            reason=reason,
            timestamp=utc_now(),
        )
        self._conn.execute(
            """
            INSERT INTO checkpoints (checkpoint_id, execution_id, task_id, state_snapshot, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.execution_id,
                checkpoint.task_id,
                json.dumps(checkpoint.state_snapshot),
                checkpoint.reason,
                checkpoint.timestamp.isoformat(),
            ),
        )
        # Also update execution's checkpoints list via update (but we already persist checkpoint separately)
        # No need to update execution row; get_execution will load checkpoints from table
        self._conn.commit()
        return copy.deepcopy(checkpoint)

    def get_checkpoints(self, execution_id: str) -> list[Checkpoint]:
        cur = self._conn.execute("SELECT * FROM checkpoints WHERE execution_id=? ORDER BY timestamp", (execution_id,))
        checkpoints: list[Checkpoint] = []
        for row in cur.fetchall():
            try:
                snapshot = json.loads(row["state_snapshot"]) if row["state_snapshot"] else {}
            except Exception:
                snapshot = {}
            checkpoints.append(
                Checkpoint(
                    checkpoint_id=row["checkpoint_id"],
                    execution_id=row["execution_id"],
                    task_id=row["task_id"],
                    state_snapshot=snapshot,
                    reason=row["reason"],
                    timestamp=_parse_dt(row["timestamp"]) or utc_now(),
                )
            )
        return [copy.deepcopy(c) for c in checkpoints]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = ["SQLiteExecutionStore"]
