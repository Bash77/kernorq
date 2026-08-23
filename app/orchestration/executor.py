from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from app.memory.store import ExecutionStore

from app.orchestration.state import (
    DependencyNotMetError,
    EventType,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    InvalidStateTransitionError,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
    utc_now,
)
from app.tools.registry import ToolNotFoundError, ToolRegistry



class ExecutionError(Exception):
    """Base exception for executor errors."""


class TaskNotFoundError(ExecutionError):
    """Raised when the specified task does not exist in the execution."""


class InvalidTaskStateError(ExecutionError):
    """Raised when the task is not in READY state for execution."""


class MissingToolError(ExecutionError):
    """Raised when a task does not have a configured tool."""


def execute_task(
    execution: Execution,
    task_id: str,
    store: ExecutionStore,
    registry: ToolRegistry,
) -> Execution:
    """Executes a task deterministically against the execution store and tool registry.

    Lifecycle:
        1. Validate execution and task exist in store.
        2. Validate task is in READY state and all dependencies succeeded.
        3. Validate task has a registered tool.
        4. Checkpoint 'before_tool_execution'.
        5. Transition task READY -> RUNNING.
        6. Record TASK_STARTED.
        7. Invoke tool (passing operation_id if accepted).
        8. Checkpoint 'after_tool_result'.
        9. On tool success: transition RUNNING -> VERIFYING, record VERIFICATION_STARTED, and return control to verifier.
        10. On tool failure/error: transition RUNNING -> FAILED, record TASK_FAILED.
        11. Persist updated execution state to store.

    Returns:
        Updated isolated Execution object from the store.
    """
    # 1. Fetch current execution from store to ensure store authority
    stored_exec = store.get_execution(execution.execution_id)

    # 2. Confirm task exists
    if task_id not in stored_exec.tasks:
        raise TaskNotFoundError(
            f"Task '{task_id}' not found in execution '{stored_exec.execution_id}'"
        )
    task: Task = stored_exec.tasks[task_id]

    # 3. Confirm dependencies
    if not task.is_ready(stored_exec.tasks):
        raise DependencyNotMetError(
            f"Task '{task_id}' dependencies are not satisfied"
        )

    # 4. Confirm task is READY
    if task.status != TaskStatus.READY:
        raise InvalidTaskStateError(
            f"Task '{task_id}' is in status '{task.status.value}', expected '{TaskStatus.READY.value}'"
        )

    # 5. Confirm tool is configured and registered
    if not task.tool_name:
        raise MissingToolError(f"Task '{task_id}' does not have a configured tool")
    if not registry.has(task.tool_name):
        raise ToolNotFoundError(
            f"Tool '{task.tool_name}' for task '{task_id}' is not in registry"
        )
    tool_func = registry.get(task.tool_name)

    # Move execution status to EXECUTING if not already
    if stored_exec.status == ExecutionStatus.PENDING:
        stored_exec.transition_to(ExecutionStatus.PLANNING)
        stored_exec.transition_to(ExecutionStatus.EXECUTING)
    elif stored_exec.status == ExecutionStatus.PLANNING:
        stored_exec.transition_to(ExecutionStatus.EXECUTING)
    elif stored_exec.status == ExecutionStatus.VERIFYING:
        stored_exec.transition_to(ExecutionStatus.EXECUTING)

    stored_exec.current_task_id = task_id

    # Persist updated state before checkpoint
    store.update_execution(stored_exec)

    # 7. Checkpoint before execution
    store.create_checkpoint(
        execution_id=stored_exec.execution_id,
        reason="before_tool_execution",
        task_id=task_id,
    )
    store.append_event(
        ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.CHECKPOINT_CREATED,
            task_id=task_id,
            metadata={"reason": "before_tool_execution"},
        )
    )

    # Re-fetch from store after checkpoint
    stored_exec = store.get_execution(stored_exec.execution_id)
    task = stored_exec.tasks[task_id]

    # 8. Transition task READY -> RUNNING, increment attempt_count
    task.transition_to(TaskStatus.RUNNING)
    task.attempt_count += 1
    stored_exec.attempt_count += 1

    # Record TASK_STARTED event
    store.append_event(
        ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.TASK_STARTED,
            task_id=task_id,
            metadata={
                "tool_name": task.tool_name,
                "attempt_count": task.attempt_count,
                "operation_id": task.operation_id,
            },
        )
    )
    store.update_execution(stored_exec)

    # 9. Invoke the tool
    tool_args = dict(task.tool_input)
    # If the tool accepts operation_id, pass it deterministically
    sig = inspect.signature(tool_func)
    if "operation_id" in sig.parameters and "operation_id" not in tool_args:
        tool_args["operation_id"] = task.operation_id

    tool_result: dict[str, Any] | None = None
    is_success = False
    is_unknown = False
    caught_error: dict[str, Any] | None = None

    try:
        raw_result = tool_func(**tool_args)
        if isinstance(raw_result, dict):
            tool_result = raw_result
            # Check for explicit success flag or error payload
            if tool_result.get("success") is False:
                is_success = False
                err = tool_result.get("error")
                if isinstance(err, dict) and err.get("type") in {"TimeoutError", "UnknownStateError"}:
                    is_unknown = True
                caught_error = err if isinstance(err, dict) else {"message": str(err or "Tool returned failure")}
            else:
                is_success = True
        else:
            tool_result = {"success": True, "data": raw_result}
            is_success = True
    except TimeoutError as exc:
        is_success = False
        is_unknown = True
        caught_error = {"type": "TimeoutError", "message": str(exc)}
        tool_result = {"success": False, "status": "unknown", "error": caught_error}
    except Exception as exc:
        is_success = False
        caught_error = {"type": type(exc).__name__, "message": str(exc)}
        tool_result = {"success": False, "error": caught_error}

    # Re-fetch from store to attach raw results
    stored_exec = store.get_execution(stored_exec.execution_id)
    task = stored_exec.tasks[task_id]
    task.result = tool_result

    # 10. Checkpoint after tool result
    store.update_execution(stored_exec)
    store.create_checkpoint(
        execution_id=stored_exec.execution_id,
        reason="after_tool_result",
        task_id=task_id,
    )
    store.append_event(
        ExecutionEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            execution_id=stored_exec.execution_id,
            event_type=EventType.CHECKPOINT_CREATED,
            task_id=task_id,
            metadata={"reason": "after_tool_result"},
        )
    )

    stored_exec = store.get_execution(stored_exec.execution_id)
    task = stored_exec.tasks[task_id]

    # 11. Handle outcomes
    if is_success:
        # Transition RUNNING -> VERIFYING and emit VERIFICATION_STARTED
        task.transition_to(TaskStatus.VERIFYING)
        stored_exec.transition_to(ExecutionStatus.VERIFYING)
        store.append_event(
            ExecutionEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                execution_id=stored_exec.execution_id,
                event_type=EventType.VERIFICATION_STARTED,
                task_id=task_id,
            )
        )
        # Note: Executor stops here for successful tool invocations.
        # It does NOT declare VERIFIED_SUCCESS, does NOT transition to SUCCEEDED,
        # does NOT emit TASK_COMPLETED, and does NOT complete the execution.
        # That responsibility belongs strictly to the verification layer (Phase 2.4).
    else:
        # Failure path
        task.error = caught_error
        stored_exec.last_error = caught_error

        # Transition RUNNING -> FAILED
        task.transition_to(TaskStatus.FAILED)
        store.append_event(
            ExecutionEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                execution_id=stored_exec.execution_id,
                event_type=EventType.TASK_FAILED,
                task_id=task_id,
                metadata={"error": caught_error, "is_unknown": is_unknown},
            )
        )

        # Update dependent tasks to BLOCKED
        stored_exec.update_task_statuses()

    # 12. Final persistence
    return store.update_execution(stored_exec)


__all__ = [
    "execute_task",
    "ExecutionError",
    "TaskNotFoundError",
    "InvalidTaskStateError",
    "MissingToolError",
]

