"""
Recovery Engine — Phase 2.5

Owns failure classification, retry bounds, UNKNOWN handling, and
the VERIFYING → RECOVERING → EXECUTING/FAILED state transitions.

Boundaries
----------
Executor : READY → RUNNING → VERIFYING (raw result, no verification)
Verifier : VERIFYING → SUCCEEDED / FAILED (deterministic evidence)
Recovery : FAILED → RECOVERING → READY/FAILED (this module, never executes tools)

Recovery never executes a tool. It only decides whether a retry is safe.
The actual execution is delegated back to executor.py.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:
    from app.memory.store import ExecutionStore

from app.orchestration.state import (
    EventType,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
    utc_now,
)


# ---------------------------------------------------------------------------
# Failure classification & recovery decision
# ---------------------------------------------------------------------------


class FailureCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    UNKNOWN = "UNKNOWN"
    PERMANENT = "PERMANENT"


class RecoveryAction(str, Enum):
    RETRY = "RETRY"
    FAIL = "FAIL"
    ESCALATE = "ESCALATE"
    # FOUND_SUCCESS is internal - external state proves operation already succeeded
    FOUND_SUCCESS = "FOUND_SUCCESS"


ExternalStateResult = Literal["FOUND", "NOT_FOUND", "UNKNOWN"]
ExternalStateChecker = Callable[[Task], ExternalStateResult]


class RecoveryError(Exception):
    """Base exception for recovery engine errors."""


class TaskNotFailedError(RecoveryError):
    """Raised when recover_task is called on a task not in FAILED state."""


PERMANENT_ERROR_TYPES = {
    "ValidationError",
    "PermanentError",
    "InvalidTaskStateError",
    "MissingToolError",
    "DuplicateToolError",
    "NotFoundError",
    "InvalidStateTransitionError",
    # Deterministic tool-precondition failures: retrying cannot change the
    # outcome because the required project state does not exist.
    "NoTestSuiteFound",
    "PytestNotAvailable",
    "InvalidPathError",
}


def classify_failure(task: Task) -> FailureCategory:
    """
    Deterministically classifies a failed task.

    Rules:
    - verification UNKNOWN or result status unknown or TimeoutError → UNKNOWN
    - verification VERIFIED_FAILURE with permanent error type → PERMANENT
    - otherwise → TRANSIENT
    """
    # UNKNOWN takes precedence
    if task.verification and task.verification.status == VerificationStatus.UNKNOWN:
        return FailureCategory.UNKNOWN
    if task.result and task.result.get("status") == "unknown":
        return FailureCategory.UNKNOWN
    if task.error and task.error.get("type") in {"TimeoutError", "UnknownStateError"}:
        return FailureCategory.UNKNOWN

    def _is_permanent(err: Any) -> bool:
        return isinstance(err, dict) and err.get("type") in PERMANENT_ERROR_TYPES

    # PERMANENT — check every surface that carries the error identity:
    # verifier evidence (post-verification path), task.error (executor path),
    # and raw tool result (executor stores result before failing).
    if task.verification and task.verification.status == VerificationStatus.VERIFIED_FAILURE:
        ev = task.verification.evidence
        if isinstance(ev, dict):
            if _is_permanent(ev.get("error")):
                return FailureCategory.PERMANENT
            if ev.get("error_type") in PERMANENT_ERROR_TYPES:
                return FailureCategory.PERMANENT
            if ev.get("explicit_outcome") == "NO_TEST_SUITE_FOUND":
                return FailureCategory.PERMANENT
    if _is_permanent(task.error):
        return FailureCategory.PERMANENT
    if isinstance(task.result, dict) and _is_permanent(task.result.get("error")):
        return FailureCategory.PERMANENT

    return FailureCategory.TRANSIENT


# ---------------------------------------------------------------------------
# Public recovery function
# ---------------------------------------------------------------------------


def recover_task(
    execution: Execution,
    task_id: str,
    store: ExecutionStore,
    external_state_checker: ExternalStateChecker | None = None,
) -> Execution:
    """
    Deterministically recovers a failed task.

    Preconditions:
        - Task must exist and be in FAILED state
        - Execution must be in VERIFYING or RECOVERING (post-verification)

    Lifecycle:
        1. Fetch fresh execution from store
        2. Validate task is FAILED
        3. Checkpoint before_recovery
        4. Emit RECOVERY_STARTED
        5. Classify failure (TRANSIENT/UNKNOWN/PERMANENT)
        6. For UNKNOWN: invoke external_state_checker if provided
           - FOUND → mark SUCCEEDED (idempotent, no retry)
           - NOT_FOUND → RETRY if attempts < max_attempts
           - UNKNOWN/ESCALATE → FAIL
        7. For TRANSIENT: RETRY if attempts < max_attempts else FAIL
        8. For PERMANENT: FAIL immediately
        9. Apply state transitions:
           - RETRY: execution VERIFYING->RECOVERING->EXECUTING, task FAILED->READY
           - FOUND_SUCCESS: task FAILED->SUCCEEDED, execution -> COMPLETED/EXECUTING
           - FAIL/ESCALATE: execution -> FAILED, task stays FAILED
        10. Record recovery_history, checkpoint after_recovery, events
        11. Persist and return isolated execution

    Never executes the tool itself.
    """
    from app.orchestration.executor import TaskNotFoundError

    stored_exec = store.get_execution(execution.execution_id)

    if task_id not in stored_exec.tasks:
        raise TaskNotFoundError(
            f"Task '{task_id}' not found in execution '{stored_exec.execution_id}'"
        )

    task = stored_exec.tasks[task_id]

    if task.status != TaskStatus.FAILED:
        raise TaskNotFailedError(
            f"Task '{task_id}' is in status '{task.status.value}', expected 'FAILED'"
        )

    # Execution must be in a recoverable state
    if stored_exec.status not in {ExecutionStatus.VERIFYING, ExecutionStatus.RECOVERING, ExecutionStatus.EXECUTING}:
        # Allow VERIFYING primarily; RECOVERING if already recovering
        # But be strict: only VERIFYING and RECOVERING are expected post-verifier
        if stored_exec.status not in {ExecutionStatus.VERIFYING, ExecutionStatus.RECOVERING}:
            raise RecoveryError(
                f"Execution '{stored_exec.execution_id}' is in status '{stored_exec.status.value}', "
                f"cannot recover task '{task_id}' from that state"
            )

    original_operation_id = task.operation_id
    previous_status = task.status.value
    previous_execution_status = stored_exec.status.value

    # Checkpoint before_recovery
    store.create_checkpoint(
        execution_id=stored_exec.execution_id,
        reason="before_recovery",
        task_id=task_id,
    )
    store.append_event(
        ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.CHECKPOINT_CREATED,
            task_id=task_id,
            actor="recovery",
            metadata={"reason": "before_recovery"},
        )
    )

    # Transition execution to RECOVERING if needed
    if stored_exec.status in {ExecutionStatus.VERIFYING, ExecutionStatus.EXECUTING}:
        stored_exec.transition_to(ExecutionStatus.RECOVERING)
    # If already RECOVERING, keep it

    store.update_execution(stored_exec)
    # Re-fetch for consistency
    stored_exec = store.get_execution(stored_exec.execution_id)
    task = stored_exec.tasks[task_id]

    # Classify
    category = classify_failure(task)

    store.append_event(
        ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.RECOVERY_STARTED,
            task_id=task_id,
            actor="recovery",
            metadata={
                "category": category.value,
                "attempt_count": task.attempt_count,
                "max_attempts": task.max_attempts,
                "previous_status": previous_status,
                "operation_id": task.operation_id,
            },
        )
    )

    # Decide recovery action
    recovery_action: RecoveryAction
    external_state: str | None = None

    if category == FailureCategory.UNKNOWN:
        if external_state_checker is None:
            recovery_action = RecoveryAction.ESCALATE
        else:
            try:
                external_state = external_state_checker(task)
            except Exception as exc:
                external_state = "UNKNOWN"
            if external_state == "FOUND":
                recovery_action = RecoveryAction.FOUND_SUCCESS
            elif external_state == "NOT_FOUND":
                if task.attempt_count < task.max_attempts:
                    recovery_action = RecoveryAction.RETRY
                else:
                    recovery_action = RecoveryAction.FAIL
            else:  # UNKNOWN
                recovery_action = RecoveryAction.ESCALATE
    elif category == FailureCategory.PERMANENT:
        recovery_action = RecoveryAction.FAIL
    else:  # TRANSIENT
        if task.attempt_count < task.max_attempts:
            recovery_action = RecoveryAction.RETRY
        else:
            recovery_action = RecoveryAction.FAIL

    # Record decision event
    store.append_event(
        ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.RECOVERY_SELECTED,
            task_id=task_id,
            actor="recovery",
            metadata={
                "category": category.value,
                "recovery_action": recovery_action.value,
                "external_state": external_state,
                "attempt_count": task.attempt_count,
                "max_attempts": task.max_attempts,
            },
        )
    )

    # Apply recovery
    timestamp = utc_now().isoformat()
    history_entry: dict[str, Any] = {
        "task_id": task_id,
        "attempt": task.attempt_count,
        "reason": category.value,
        "previous_status": previous_status,
        "previous_execution_status": previous_execution_status,
        "recovery_action": recovery_action.value,
        "external_state": external_state,
        "timestamp": timestamp,
        "operation_id": original_operation_id,
    }

    if recovery_action == RecoveryAction.RETRY:
        # Task FAILED -> READY
        task.transition_to(TaskStatus.READY)
        # Ensure operation_id preserved
        assert task.operation_id == original_operation_id

        # Execution RECOVERING -> EXECUTING
        stored_exec.transition_to(ExecutionStatus.EXECUTING)

        history_entry["result"] = "RETRY_SCHEDULED"
        stored_exec.recovery_history.append(history_entry)

        store.append_event(
            ExecutionEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                execution_id=stored_exec.execution_id,
                event_type=EventType.RETRY_STARTED,
                task_id=task_id,
                actor="recovery",
                metadata={
                    "attempt_count": task.attempt_count,
                    "max_attempts": task.max_attempts,
                    "operation_id": task.operation_id,
                },
            )
        )

        # Unlock? READY tasks don't need downstream update yet; but ensure blocked children stay blocked until retry succeeds
        stored_exec.update_task_statuses()

    elif recovery_action == RecoveryAction.FOUND_SUCCESS:
        # Idempotent success: external state proves operation already succeeded
        verification = VerificationResult(
            status=VerificationStatus.VERIFIED_SUCCESS,
            message="Recovery verified external state: operation FOUND, treating as success",
            evidence={
                "task_id": task.task_id,
                "operation_id": task.operation_id,
                "recovery": "external_state_FOUND",
                "category": category.value,
            },
            timestamp=utc_now(),
        )
        task.verification = verification
        stored_exec.verification_results.append(verification)
        # FAILED -> SUCCEEDED (allowed for recovery)
        task.transition_to(TaskStatus.SUCCEEDED)
        assert task.operation_id == original_operation_id

        history_entry["result"] = "RECOVERED_AS_SUCCESS"
        stored_exec.recovery_history.append(history_entry)

        store.append_event(
            ExecutionEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                execution_id=stored_exec.execution_id,
                event_type=EventType.VERIFICATION_SUCCEEDED,
                task_id=task_id,
                actor="recovery",
                metadata={"verification": verification.to_dict(), "recovered_via": "FOUND"},
            )
        )
        store.append_event(
            ExecutionEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                execution_id=stored_exec.execution_id,
                event_type=EventType.TASK_COMPLETED,
                task_id=task_id,
                actor="recovery",
                metadata={"result": task.result},
            )
        )

        stored_exec.update_task_statuses()

        # Check if all tasks succeeded -> COMPLETED, else continue EXECUTING
        all_succeeded = all(t.status == TaskStatus.SUCCEEDED for t in stored_exec.tasks.values())
        if all_succeeded:
            stored_exec.transition_to(ExecutionStatus.COMPLETED)
            store.append_event(
                ExecutionEvent(
                    event_id=f"evt_{uuid.uuid4().hex[:12]}",
                    execution_id=stored_exec.execution_id,
                    event_type=EventType.EXECUTION_COMPLETED,
                    actor="recovery",
                )
            )
        else:
            # Not all done, continue execution for remaining tasks
            try:
                stored_exec.transition_to(ExecutionStatus.EXECUTING)
            except Exception:
                # If already COMPLETED, ignore
                pass

    else:  # FAIL or ESCALATE
        # Permanently failed
        history_entry["result"] = "FAILED_PERMANENTLY" if recovery_action == RecoveryAction.FAIL else "ESCALATED_UNKNOWN"
        stored_exec.recovery_history.append(history_entry)
        stored_exec.last_error = {
            "type": "RecoveryFailed",
            "message": f"Task {task_id} failed permanently after {task.attempt_count} attempts (category={category.value}, action={recovery_action.value})",
            "task_error": task.error,
            "verification": task.verification.to_dict() if task.verification else None,
            "category": category.value,
            "recovery_action": recovery_action.value,
            "external_state": external_state,
        }
        # Execution RECOVERING -> FAILED
        try:
            stored_exec.transition_to(ExecutionStatus.FAILED)
        except Exception:
            # If already FAILED, ignore
            pass

        store.append_event(
            ExecutionEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                execution_id=stored_exec.execution_id,
                event_type=EventType.EXECUTION_FAILED,
                task_id=task_id,
                actor="recovery",
                metadata={
                    "category": category.value,
                    "recovery_action": recovery_action.value,
                    "attempt_count": task.attempt_count,
                },
            )
        )
        stored_exec.update_task_statuses()

    # Checkpoint after_recovery
    store.create_checkpoint(
        execution_id=stored_exec.execution_id,
        reason="after_recovery",
        task_id=task_id,
    )
    store.append_event(
        ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.CHECKPOINT_CREATED,
            task_id=task_id,
            actor="recovery",
            metadata={"reason": "after_recovery"},
        )
    )

    return store.update_execution(stored_exec)


__all__ = [
    "FailureCategory",
    "RecoveryAction",
    "RecoveryError",
    "TaskNotFailedError",
    "classify_failure",
    "recover_task",
]
