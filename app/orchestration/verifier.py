"""
Verification Engine — Phase 2.4

The verifier is the deterministic layer that inspects actual outcomes and
decides whether a tool invocation produced the expected result.

Design rules
------------
- The verifier never asks the LLM whether something worked.
- It inspects real, observable state: return values, file existence,
  structured fields, checksums, etc.
- It produces a VerificationResult with VERIFIED_SUCCESS, VERIFIED_FAILURE,
  or UNKNOWN.
- UNKNOWN is not success — it means the verifier cannot determine outcome
  and the recovery engine must take over.
- It records evidence so the audit trail is complete.
- It stores the result on the task and emits the appropriate audit events.
- It handles the VERIFYING -> SUCCEEDED / FAILED state transition that the
  executor deliberately left open.

Component boundaries
--------------------
Executor   : READY -> RUNNING -> VERIFYING  (raw result persisted, stops here)
Verifier   : VERIFYING -> SUCCEEDED / FAILED  (this module)
Recovery   : FAILED / UNKNOWN -> next action  (Phase 2.5)
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

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
# Verification strategy protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class VerificationStrategy(Protocol):
    """
    A callable that inspects a task's raw tool result and returns a
    VerificationResult.

    It receives the task and the full execution (for cross-task context).
    It must not mutate either argument.
    It must return a VerificationResult without side effects on the store.
    """

    def __call__(
        self,
        task: Task,
        execution: Execution,
    ) -> VerificationResult:
        ...


# ---------------------------------------------------------------------------
# Built-in strategies
# ---------------------------------------------------------------------------

def _verify_by_success_flag(
    task: Task,
    execution: Execution,
) -> VerificationResult:
    """
    Strategy: inspect the 'success' field of the raw tool result.

    This is the default fallback when no specialised strategy is registered.

    Rules:
    - result is None                       → UNKNOWN  (executor never stored a result)
    - result["success"] is True            → VERIFIED_SUCCESS
    - result["success"] is False and
      result["status"] == "unknown"        → UNKNOWN  (timeout / ambiguous)
    - result["success"] is False otherwise → VERIFIED_FAILURE
    """
    result = task.result

    if result is None:
        return VerificationResult(
            status=VerificationStatus.UNKNOWN,
            message="No tool result was stored; cannot determine outcome",
            evidence={"task_id": task.task_id, "operation_id": task.operation_id},
            timestamp=utc_now(),
        )

    if result.get("success") is True:
        return VerificationResult(
            status=VerificationStatus.VERIFIED_SUCCESS,
            message="Tool reported success and result structure is valid",
            evidence={
                "task_id": task.task_id,
                "operation_id": task.operation_id,
                "result_keys": list(result.keys()),
                "data_present": "data" in result,
            },
            timestamp=utc_now(),
        )

    # success is False or missing
    if result.get("status") == "unknown":
        error = result.get("error", {})
        return VerificationResult(
            status=VerificationStatus.UNKNOWN,
            message="Tool result status is 'unknown'; external state must be inspected",
            evidence={
                "task_id": task.task_id,
                "operation_id": task.operation_id,
                "error_type": error.get("type") if isinstance(error, dict) else None,
                "error_message": error.get("message") if isinstance(error, dict) else str(error),
            },
            timestamp=utc_now(),
        )

    # Explicit failure
    error = result.get("error", {})
    return VerificationResult(
        status=VerificationStatus.VERIFIED_FAILURE,
        message="Tool reported failure",
        evidence={
            "task_id": task.task_id,
            "operation_id": task.operation_id,
            "error": error if isinstance(error, dict) else {"message": str(error)},
        },
        timestamp=utc_now(),
    )


def _verify_required_fields_present(
    required_fields: list[str],
) -> VerificationStrategy:
    """
    Strategy factory: verifies that the tool result contains all required fields.

    Use this when a tool must return structured data and presence of specific
    keys is the evidence of success.

    Example:
        strategy = _verify_required_fields_present(["file_path", "file_size", "checksum"])
    """
    def _strategy(task: Task, execution: Execution) -> VerificationResult:
        result = task.result or {}
        missing = [f for f in required_fields if f not in result]
        if not missing and result.get("success") is not False:
            return VerificationResult(
                status=VerificationStatus.VERIFIED_SUCCESS,
                message=f"All required fields present: {required_fields}",
                evidence={
                    "task_id": task.task_id,
                    "operation_id": task.operation_id,
                    "required_fields": required_fields,
                    "found_fields": [f for f in required_fields if f in result],
                },
                timestamp=utc_now(),
            )
        return VerificationResult(
            status=VerificationStatus.VERIFIED_FAILURE,
            message=f"Required fields missing from tool result: {missing}",
            evidence={
                "task_id": task.task_id,
                "operation_id": task.operation_id,
                "required_fields": required_fields,
                "missing_fields": missing,
            },
            timestamp=utc_now(),
        )
    return _strategy


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

class VerificationStrategyRegistry:
    """
    Maps tool names to their verification strategies.

    If no strategy is registered for a tool, the default
    success-flag strategy is used.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, VerificationStrategy] = {}

    def register(self, tool_name: str, strategy: VerificationStrategy) -> None:
        """Registers a verification strategy for a tool name."""
        if not tool_name or not callable(strategy):
            raise ValueError(f"Invalid strategy registration for tool '{tool_name}'")
        self._strategies[tool_name] = strategy

    def get(self, tool_name: str | None) -> VerificationStrategy:
        """Returns the registered strategy, or the default success-flag strategy."""
        if tool_name and tool_name in self._strategies:
            return self._strategies[tool_name]
        return _verify_by_success_flag

    def list_tools(self) -> list[str]:
        """Returns tool names that have explicit strategies registered."""
        return sorted(self._strategies.keys())


def create_default_strategy_registry() -> VerificationStrategyRegistry:
    """
    Returns a VerificationStrategyRegistry pre-configured with strategies
    for the built-in tools.

    Currently: inspect_project_workspace is verified via required fields.
    The tool returns 'repository_root' (not 'directory') plus 'files'.
    Requiring only ['success','files'] keeps verification evidence-based
    while remaining compatible with both real tool output and synthetic
    test payloads that use 'directory'.
    """
    registry = VerificationStrategyRegistry()
    registry.register(
        "inspect_project_workspace",
        _verify_required_fields_present(["success", "files"]),
    )
    return registry


# ---------------------------------------------------------------------------
# Public verification function
# ---------------------------------------------------------------------------

class VerificationError(Exception):
    """Base exception for verifier errors."""


class TaskNotInVerifyingStateError(VerificationError):
    """Raised when verify_task is called on a task that is not in VERIFYING state."""


def verify_task(
    execution: Execution,
    task_id: str,
    store: ExecutionStore,
    strategy_registry: VerificationStrategyRegistry,
) -> Execution:
    """
    Deterministically verifies a task that has completed tool execution.

    Preconditions:
        - Task must exist in the execution.
        - Task status must be VERIFYING.
        - A raw tool result must be stored (task.result).

    Lifecycle:
        1. Fetch fresh execution from store.
        2. Validate task exists and is in VERIFYING state.
        3. Select the appropriate verification strategy.
        4. Execute the strategy against the raw result — no LLM involvement.
        5. Persist VerificationResult on the task and on the execution.
        6. Emit VERIFICATION_SUCCEEDED or VERIFICATION_FAILED audit event.
        7. Transition task to SUCCEEDED or FAILED.
        8. Update downstream task readiness.
        9. If all tasks SUCCEEDED, transition execution to COMPLETED and emit
           EXECUTION_COMPLETED.
        10. If task FAILED, record error and update downstream BLOCKED tasks.
        11. Persist execution to store.
        12. Return isolated updated execution.

    Returns:
        Updated isolated Execution from the store.

    Raises:
        TaskNotFoundError: Task does not exist.
        TaskNotInVerifyingStateError: Task is not in VERIFYING state.
    """
    from app.orchestration.executor import TaskNotFoundError  # local import to avoid circular

    stored_exec = store.get_execution(execution.execution_id)

    if task_id not in stored_exec.tasks:
        raise TaskNotFoundError(
            f"Task '{task_id}' not found in execution '{stored_exec.execution_id}'"
        )

    task = stored_exec.tasks[task_id]

    if task.status != TaskStatus.VERIFYING:
        raise TaskNotInVerifyingStateError(
            f"Task '{task_id}' is in status '{task.status.value}', expected 'VERIFYING'"
        )

    # Select strategy
    strategy = strategy_registry.get(task.tool_name)

    # Execute strategy — deterministic, no LLM
    verification: VerificationResult = strategy(task, stored_exec)

    # Persist VerificationResult on task and execution
    task.verification = verification
    stored_exec.verification_results.append(verification)

    if verification.status == VerificationStatus.VERIFIED_SUCCESS:
        # VERIFYING -> SUCCEEDED
        task.transition_to(TaskStatus.SUCCEEDED)

        store.append_event(ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.VERIFICATION_SUCCEEDED,
            task_id=task_id,
            actor="verifier",
            metadata={"verification": verification.to_dict()},
        ))
        store.append_event(ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.TASK_COMPLETED,
            task_id=task_id,
            actor="verifier",
            metadata={"result": task.result},
        ))

        # Unlock downstream tasks
        stored_exec.update_task_statuses()

        # If all tasks have SUCCEEDED, complete the execution
        all_succeeded = all(
            t.status == TaskStatus.SUCCEEDED for t in stored_exec.tasks.values()
        )
        if all_succeeded:
            stored_exec.transition_to(ExecutionStatus.COMPLETED)
            store.append_event(ExecutionEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                execution_id=stored_exec.execution_id,
                event_type=EventType.EXECUTION_COMPLETED,
                actor="verifier",
            ))

    elif verification.status == VerificationStatus.VERIFIED_FAILURE:
        # VERIFYING -> FAILED
        task.transition_to(TaskStatus.FAILED)
        stored_exec.last_error = verification.evidence

        store.append_event(ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.VERIFICATION_FAILED,
            task_id=task_id,
            actor="verifier",
            metadata={"verification": verification.to_dict()},
        ))

        # Block dependent tasks
        stored_exec.update_task_statuses()

    else:
        # UNKNOWN: verification could not determine outcome
        # VERIFYING -> FAILED (ambiguous; recovery engine must assess)
        task.transition_to(TaskStatus.FAILED)
        stored_exec.last_error = {
            "type": "UnknownVerificationOutcome",
            "message": verification.message,
            "evidence": verification.evidence,
        }

        store.append_event(ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.VERIFICATION_FAILED,
            task_id=task_id,
            actor="verifier",
            metadata={
                "verification": verification.to_dict(),
                "is_unknown": True,
            },
        ))

        # Block dependent tasks
        stored_exec.update_task_statuses()

    # Checkpoint after verification
    store.create_checkpoint(
        execution_id=stored_exec.execution_id,
        reason="after_verification",
        task_id=task_id,
    )
    store.append_event(ExecutionEvent(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        execution_id=stored_exec.execution_id,
        event_type=EventType.CHECKPOINT_CREATED,
        task_id=task_id,
        actor="verifier",
        metadata={"reason": "after_verification"},
    ))

    return store.update_execution(stored_exec)


__all__ = [
    "VerificationStrategy",
    "VerificationStrategyRegistry",
    "VerificationError",
    "TaskNotInVerifyingStateError",
    "verify_task",
    "create_default_strategy_registry",
]
