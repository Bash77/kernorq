from __future__ import annotations

from typing import Any
import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.executor import (
    InvalidTaskStateError,
    MissingToolError,
    TaskNotFoundError,
    execute_task,
)
from app.orchestration.state import (
    DependencyNotMetError,
    EventType,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
)
from app.tools.registry import (
    DuplicateToolError,
    ToolNotFoundError,
    ToolRegistry,
    create_default_tool_registry,
)


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

def test_tool_registry_registration_and_retrieval():
    registry = ToolRegistry()

    def sample_tool(param: str) -> dict[str, Any]:
        return {"success": True, "value": param}

    registry.register("sample_tool", sample_tool)
    assert registry.has("sample_tool") is True
    assert registry.has("missing_tool") is False
    assert registry.get("sample_tool") == sample_tool
    assert registry.list_tools() == ["sample_tool"]

    # Duplicate registration error
    with pytest.raises(DuplicateToolError, match="already registered"):
        registry.register("sample_tool", sample_tool)

    # Tool not found error
    with pytest.raises(ToolNotFoundError, match="not found in registry"):
        registry.get("missing_tool")


# ---------------------------------------------------------------------------
# Successful Execution — enforces execution/verification separation
# ---------------------------------------------------------------------------

def test_executor_successful_tool_execution():
    """
    After a successful tool invocation the executor MUST:
      - persist the raw result
      - leave the task in VERIFYING (not SUCCEEDED)
      - NOT set task.verification (that is the verifier's job)
      - NOT mark the execution COMPLETED
      - emit VERIFICATION_STARTED but NOT VERIFICATION_SUCCEEDED or TASK_COMPLETED
    """
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_succ", objective="Inspect workspace")
    task = Task(
        task_id="task_inspect",
        execution_id="exec_succ",
        title="Inspect Workspace Task",
        description="Run inspect_project_workspace",
        tool_name="inspect_project_workspace",
        tool_input={"directory_path": "."},
    )
    execution.add_task(task)
    execution.update_task_statuses()
    assert task.status == TaskStatus.READY

    store.create_execution(execution)

    updated_exec = execute_task(execution, "task_inspect", store, registry)
    task_res = updated_exec.tasks["task_inspect"]

    # Tool was executed and raw result was persisted
    assert task_res.attempt_count == 1
    assert task_res.result is not None
    assert task_res.result["success"] is True

    # Task must be in VERIFYING — executor does NOT push to SUCCEEDED
    assert task_res.status == TaskStatus.VERIFYING

    # Verification is the verifier's responsibility; executor leaves it None
    assert task_res.verification is None

    # Execution must NOT be marked COMPLETED — verifier decides that
    assert updated_exec.status != ExecutionStatus.COMPLETED

    # Audit events: expected
    events = store.get_events("exec_succ")
    event_types = [e.event_type for e in events]
    assert EventType.TASK_STARTED in event_types
    assert EventType.CHECKPOINT_CREATED in event_types
    assert EventType.VERIFICATION_STARTED in event_types

    # Audit events: must NOT exist
    assert EventType.VERIFICATION_SUCCEEDED not in event_types
    assert EventType.TASK_COMPLETED not in event_types
    assert EventType.EXECUTION_COMPLETED not in event_types


# ---------------------------------------------------------------------------
# Unknown / unregistered tool
# ---------------------------------------------------------------------------

def test_executor_unknown_tool():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_unk_tool", objective="Unknown tool test")
    task = Task(
        task_id="task_unk",
        execution_id="exec_unk_tool",
        title="Unknown tool",
        description="Call non-existent tool",
        tool_name="non_existent_tool_xyz",
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    with pytest.raises(ToolNotFoundError, match="not in registry"):
        execute_task(execution, "task_unk", store, registry)


def test_executor_missing_tool():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_no_tool", objective="Missing tool test")
    task = Task(
        task_id="task_no_tool",
        execution_id="exec_no_tool",
        title="No tool task",
        description="Missing tool",
        tool_name=None,
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    with pytest.raises(MissingToolError, match="does not have a configured tool"):
        execute_task(execution, "task_no_tool", store, registry)


# ---------------------------------------------------------------------------
# Invalid pre-conditions
# ---------------------------------------------------------------------------

def test_executor_invalid_task_state():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_inv_state", objective="Invalid task state")
    task = Task(
        task_id="task_pending",
        execution_id="exec_inv_state",
        title="Pending task",
        description="Not ready yet",
        tool_name="inspect_project_workspace",
        status=TaskStatus.PENDING,
    )
    execution.add_task(task)
    store.create_execution(execution)

    with pytest.raises(InvalidTaskStateError, match="expected 'READY'"):
        execute_task(execution, "task_pending", store, registry)


def test_executor_dependency_not_met():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_dep", objective="Dep test")
    task_a = Task(
        task_id="task_a",
        execution_id="exec_dep",
        title="Task A",
        description="A",
        tool_name="inspect_project_workspace",
    )
    task_b = Task(
        task_id="task_b",
        execution_id="exec_dep",
        title="Task B",
        description="B",
        tool_name="inspect_project_workspace",
        dependencies=["task_a"],
        status=TaskStatus.READY,  # Artificially set to READY to test dependency check
    )
    execution.add_task(task_a)
    execution.add_task(task_b)
    store.create_execution(execution)

    with pytest.raises(DependencyNotMetError, match="dependencies are not satisfied"):
        execute_task(execution, "task_b", store, registry)


# ---------------------------------------------------------------------------
# Failure path — verification is NOT set by executor on failures either
# ---------------------------------------------------------------------------

def test_executor_tool_exception_handling():
    """
    On a tool exception the executor must:
      - transition task to FAILED
      - persist raw error on task.error and task.result
      - NOT set task.verification (verifier's responsibility)
      - NOT auto-retry
    """
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def failing_tool():
        raise RuntimeError("Database connection failed")

    registry.register("failing_tool", failing_tool)

    execution = Execution(execution_id="exec_fail", objective="Fail test")
    task = Task(
        task_id="task_fail",
        execution_id="exec_fail",
        title="Failing Task",
        description="Will raise exception",
        tool_name="failing_tool",
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    updated_exec = execute_task(execution, "task_fail", store, registry)
    task_res = updated_exec.tasks["task_fail"]

    # Task FAILED with raw error preserved
    assert task_res.status == TaskStatus.FAILED
    assert task_res.error is not None
    assert task_res.error["type"] == "RuntimeError"
    assert "Database connection failed" in task_res.error["message"]

    # Executor does NOT set verification on failures either
    assert task_res.verification is None


def test_executor_tool_failure_result():
    """
    When a tool returns success: False the executor must persist the raw
    result, transition to FAILED, and leave verification to the verifier.
    """
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_tool_fail", objective="Tool fail test")
    task = Task(
        task_id="task_dir_fail",
        execution_id="exec_tool_fail",
        title="Inspect non-existent",
        description="Inspect missing path",
        tool_name="inspect_project_workspace",
        tool_input={"directory_path": "./non_existent_folder_99999"},
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    updated_exec = execute_task(execution, "task_dir_fail", store, registry)
    task_res = updated_exec.tasks["task_dir_fail"]

    # Task FAILED with raw result preserved
    assert task_res.status == TaskStatus.FAILED
    assert task_res.result["success"] is False
    assert task_res.error["type"] == "DirectoryNotFoundError"

    # Executor does NOT set verification
    assert task_res.verification is None


# ---------------------------------------------------------------------------
# Checkpoints and audit trail
# ---------------------------------------------------------------------------

def test_executor_checkpoints_created():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_chk_test", objective="Checkpoint verification")
    task = Task(
        task_id="t_chk",
        execution_id="exec_chk_test",
        title="Checkpoints Task",
        description="Verify checkpoints",
        tool_name="inspect_project_workspace",
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    execute_task(execution, "t_chk", store, registry)

    checkpoints = store.get_checkpoints("exec_chk_test")
    assert len(checkpoints) >= 2
    reasons = [c.reason for c in checkpoints]
    assert "before_tool_execution" in reasons
    assert "after_tool_result" in reasons


def test_executor_audit_events_created():
    """
    After a successful tool invocation the executor must emit:
      CHECKPOINT_CREATED, TASK_STARTED, VERIFICATION_STARTED

    It must NOT emit:
      VERIFICATION_SUCCEEDED, TASK_COMPLETED, EXECUTION_COMPLETED
    """
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()

    execution = Execution(execution_id="exec_evt_test", objective="Audit events verification")
    task = Task(
        task_id="t_evt",
        execution_id="exec_evt_test",
        title="Events Task",
        description="Verify audit events",
        tool_name="inspect_project_workspace",
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    execute_task(execution, "t_evt", store, registry)

    events = store.get_events("exec_evt_test")
    event_types = [e.event_type for e in events]

    # Must be present
    assert EventType.TASK_STARTED in event_types
    assert EventType.CHECKPOINT_CREATED in event_types
    assert EventType.VERIFICATION_STARTED in event_types

    # Must NOT be present — these belong to the verification layer
    assert EventType.VERIFICATION_SUCCEEDED not in event_types
    assert EventType.TASK_COMPLETED not in event_types
    assert EventType.EXECUTION_COMPLETED not in event_types


# ---------------------------------------------------------------------------
# Operation ID stability across retries
# ---------------------------------------------------------------------------

def test_stable_operation_id_across_attempts():
    """
    The same operation_id must be passed to the tool on every attempt,
    enabling external idempotency. After a retry succeeds the task must
    be in VERIFYING (not SUCCEEDED) since verification is separate.
    """
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    received_op_ids: list[str] = []

    def op_aware_tool(operation_id: str):
        received_op_ids.append(operation_id)
        if len(received_op_ids) == 1:
            raise RuntimeError("Transient failure attempt 1")
        return {"success": True, "op": operation_id}

    registry.register("op_aware_tool", op_aware_tool)

    execution = Execution(execution_id="exec_op_test", objective="Operation id test")
    task = Task(
        task_id="t_op",
        execution_id="exec_op_test",
        title="Op Task",
        description="Op ID test",
        tool_name="op_aware_tool",
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    initial_op_id = task.operation_id

    # Attempt 1 -> fails
    exec_after_1 = execute_task(execution, "t_op", store, registry)
    task_after_1 = exec_after_1.tasks["t_op"]
    assert task_after_1.status == TaskStatus.FAILED
    assert task_after_1.attempt_count == 1
    assert task_after_1.operation_id == initial_op_id

    # Retry preparation (FAILED -> READY via AGENTS.md-mandated two-step)
    task_after_1.transition_to(TaskStatus.READY)
    store.update_execution(exec_after_1)

    # Attempt 2 -> tool succeeds; executor leaves task in VERIFYING
    exec_after_2 = execute_task(exec_after_1, "t_op", store, registry)
    task_after_2 = exec_after_2.tasks["t_op"]

    # Executor hands off to verifier; task is VERIFYING not SUCCEEDED
    assert task_after_2.status == TaskStatus.VERIFYING
    assert task_after_2.attempt_count == 2
    assert task_after_2.operation_id == initial_op_id
    # Same operation_id was supplied on both attempts
    assert received_op_ids == [initial_op_id, initial_op_id]


# ---------------------------------------------------------------------------
# Timeout / unknown outcome
# ---------------------------------------------------------------------------

def test_unknown_result_handling():
    """
    A TimeoutError must:
      - transition the task to FAILED (not VERIFYING — the outcome is
        ambiguous but the task cannot remain live)
      - preserve the unknown-outcome information in task.result
      - NOT set task.verification (verifier/recovery layer's responsibility)
      - NOT auto-retry
    """
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def timeout_tool():
        raise TimeoutError("External API request timed out with unknown state")

    registry.register("timeout_tool", timeout_tool)

    execution = Execution(execution_id="exec_timeout", objective="Timeout test")
    task = Task(
        task_id="t_timeout",
        execution_id="exec_timeout",
        title="Timeout Task",
        description="Simulate timeout",
        tool_name="timeout_tool",
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    updated_exec = execute_task(execution, "t_timeout", store, registry)
    task_res = updated_exec.tasks["t_timeout"]

    assert task_res.status == TaskStatus.FAILED
    # Unknown-outcome information is preserved for the recovery layer
    assert task_res.result["status"] == "unknown"
    assert task_res.result["error"]["type"] == "TimeoutError"
    # Executor does NOT set verification — that is the verifier/recovery layer's job
    assert task_res.verification is None
    # Must NOT have auto-retried or progressed
    assert task_res.attempt_count == 1

