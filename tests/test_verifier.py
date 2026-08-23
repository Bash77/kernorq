"""
Tests for Phase 2.4 — Verification Engine

Coverage:
  1. Strategy registry: registration, fallback to default, list_tools
  2. Default strategy (success flag): success / failure / unknown / missing result
  3. Required-fields strategy: fields present / missing / explicit failure
  4. verify_task happy path: VERIFYING -> SUCCEEDED, evidence, VERIFICATION_SUCCEEDED,
     TASK_COMPLETED, after_verification checkpoint
  5. verify_task single-task execution: all-succeeded triggers EXECUTION_COMPLETED
  6. verify_task failure path: VERIFYING -> FAILED, VERIFICATION_FAILED, no TASK_COMPLETED
  7. verify_task unknown outcome: VERIFYING -> FAILED, is_unknown=True in event metadata,
     last_error captured
  8. verify_task rejects non-VERIFYING tasks with TaskNotInVerifyingStateError
  9. verify_task rejects missing task with TaskNotFoundError
  10. Full execute + verify pipeline: executor stops at VERIFYING, verifier completes
  11. Multi-task execution: second task unlocked only after first verified
  12. verify_task produces after_verification checkpoint
  13. No false positive: custom strategy can block success even when tool returned True
"""
from __future__ import annotations

from typing import Any

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.executor import TaskNotFoundError, execute_task
from app.orchestration.state import (
    EventType,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from app.orchestration.verifier import (
    TaskNotInVerifyingStateError,
    VerificationStrategyRegistry,
    create_default_strategy_registry,
    verify_task,
)
from app.tools.registry import ToolRegistry, create_default_tool_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store_with_exec(execution: Execution) -> InMemoryExecutionStore:
    store = InMemoryExecutionStore()
    store.create_execution(execution)
    return store


def _make_single_task_exec(
    exec_id: str,
    task_id: str,
    tool_name: str = "mock_tool",
    tool_input: dict | None = None,
) -> tuple[Execution, Task]:
    execution = Execution(execution_id=exec_id, objective="Test")
    task = Task(
        task_id=task_id,
        execution_id=exec_id,
        title="Test task",
        description="Test",
        tool_name=tool_name,
        tool_input=tool_input or {},
    )
    execution.add_task(task)
    execution.update_task_statuses()
    return execution, task


def _make_verifying_task(
    store: InMemoryExecutionStore,
    exec_id: str,
    task_id: str,
    result: dict[str, Any],
    tool_name: str = "mock_tool",
) -> Execution:
    """
    Directly inserts an execution with a task already in VERIFYING state
    and a pre-set raw result, bypassing the executor. Used to test the
    verifier in isolation.
    """
    execution = Execution(execution_id=exec_id, objective="Verify test")
    task = Task(
        task_id=task_id,
        execution_id=exec_id,
        title="Verifying task",
        description="Already verifying",
        tool_name=tool_name,
        status=TaskStatus.VERIFYING,
    )
    task.result = result
    execution.add_task(task)
    execution.status = ExecutionStatus.VERIFYING
    store.create_execution(execution)
    return store.get_execution(exec_id)


# ---------------------------------------------------------------------------
# 1. Strategy registry
# ---------------------------------------------------------------------------

def test_strategy_registry_registration_and_fallback():
    registry = VerificationStrategyRegistry()

    # No strategies registered — returns default
    default = registry.get("any_tool")
    assert callable(default)
    assert registry.list_tools() == []

    # Register a custom strategy
    custom_calls: list = []
    def custom_strategy(task, execution) -> VerificationResult:
        custom_calls.append(task.task_id)
        return VerificationResult(
            status=VerificationStatus.VERIFIED_SUCCESS,
            message="custom",
            evidence={},
        )

    registry.register("my_tool", custom_strategy)
    assert "my_tool" in registry.list_tools()
    assert registry.get("my_tool") is custom_strategy
    # Different tool still returns default
    assert registry.get("other_tool") is not custom_strategy


def test_strategy_registry_rejects_bad_registration():
    registry = VerificationStrategyRegistry()
    with pytest.raises(ValueError):
        registry.register("", lambda t, e: None)  # empty name
    with pytest.raises(ValueError):
        registry.register("my_tool", "not_callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. Default strategy (success flag)
# ---------------------------------------------------------------------------

def test_default_strategy_success():
    from app.orchestration.verifier import _verify_by_success_flag
    execution, task = _make_single_task_exec("e1", "t1")
    task.result = {"success": True, "data": {"count": 3}}
    result = _verify_by_success_flag(task, execution)
    assert result.status == VerificationStatus.VERIFIED_SUCCESS
    assert result.evidence["task_id"] == "t1"


def test_default_strategy_failure():
    from app.orchestration.verifier import _verify_by_success_flag
    execution, task = _make_single_task_exec("e2", "t2")
    task.result = {"success": False, "error": {"type": "RuntimeError", "message": "boom"}}
    result = _verify_by_success_flag(task, execution)
    assert result.status == VerificationStatus.VERIFIED_FAILURE


def test_default_strategy_unknown_timeout():
    from app.orchestration.verifier import _verify_by_success_flag
    execution, task = _make_single_task_exec("e3", "t3")
    task.result = {"success": False, "status": "unknown", "error": {"type": "TimeoutError", "message": "timed out"}}
    result = _verify_by_success_flag(task, execution)
    assert result.status == VerificationStatus.UNKNOWN
    assert "TimeoutError" in str(result.evidence)


def test_default_strategy_missing_result():
    from app.orchestration.verifier import _verify_by_success_flag
    execution, task = _make_single_task_exec("e4", "t4")
    task.result = None
    result = _verify_by_success_flag(task, execution)
    assert result.status == VerificationStatus.UNKNOWN
    assert "No tool result" in result.message


# ---------------------------------------------------------------------------
# 3. Required-fields strategy
# ---------------------------------------------------------------------------

def test_required_fields_strategy_all_present():
    from app.orchestration.verifier import _verify_required_fields_present
    execution, task = _make_single_task_exec("e5", "t5")
    task.result = {"success": True, "directory": "/app", "files": ["a.py"]}
    strategy = _verify_required_fields_present(["success", "directory", "files"])
    result = strategy(task, execution)
    assert result.status == VerificationStatus.VERIFIED_SUCCESS
    assert result.evidence["found_fields"] == ["success", "directory", "files"]


def test_required_fields_strategy_missing():
    from app.orchestration.verifier import _verify_required_fields_present
    execution, task = _make_single_task_exec("e6", "t6")
    task.result = {"success": True, "directory": "/app"}  # 'files' missing
    strategy = _verify_required_fields_present(["success", "directory", "files"])
    result = strategy(task, execution)
    assert result.status == VerificationStatus.VERIFIED_FAILURE
    assert "files" in result.evidence["missing_fields"]


def test_required_fields_strategy_explicit_failure():
    from app.orchestration.verifier import _verify_required_fields_present
    execution, task = _make_single_task_exec("e7", "t7")
    task.result = {"success": False, "directory": "/app", "files": []}
    strategy = _verify_required_fields_present(["success", "directory", "files"])
    result = strategy(task, execution)
    # success: False overrides field presence
    assert result.status == VerificationStatus.VERIFIED_FAILURE


# ---------------------------------------------------------------------------
# 4. verify_task happy path
# ---------------------------------------------------------------------------

def test_verify_task_success():
    store = InMemoryExecutionStore()
    execution = _make_verifying_task(
        store, "exec_v1", "task_v1",
        result={"success": True, "directory": ".", "files": ["app"]},
        tool_name="inspect_project_workspace",
    )
    strategy_registry = create_default_strategy_registry()

    updated_exec = verify_task(execution, "task_v1", store, strategy_registry)
    task = updated_exec.tasks["task_v1"]

    assert task.status == TaskStatus.SUCCEEDED
    assert task.verification is not None
    assert task.verification.status == VerificationStatus.VERIFIED_SUCCESS
    assert task.verification.evidence is not None

    event_types = [e.event_type for e in store.get_events("exec_v1")]
    assert EventType.VERIFICATION_SUCCEEDED in event_types
    assert EventType.TASK_COMPLETED in event_types


# ---------------------------------------------------------------------------
# 5. Single-task execution completes the execution
# ---------------------------------------------------------------------------

def test_verify_task_completes_execution_when_all_succeeded():
    store = InMemoryExecutionStore()
    execution = _make_verifying_task(
        store, "exec_v2", "task_v2",
        result={"success": True, "directory": ".", "files": ["app"]},
        tool_name="inspect_project_workspace",
    )
    strategy_registry = create_default_strategy_registry()

    updated_exec = verify_task(execution, "task_v2", store, strategy_registry)

    assert updated_exec.status == ExecutionStatus.COMPLETED
    event_types = [e.event_type for e in store.get_events("exec_v2")]
    assert EventType.EXECUTION_COMPLETED in event_types


# ---------------------------------------------------------------------------
# 6. verify_task failure path
# ---------------------------------------------------------------------------

def test_verify_task_failure():
    store = InMemoryExecutionStore()
    execution = _make_verifying_task(
        store, "exec_v3", "task_v3",
        result={"success": False, "error": {"type": "NotFoundError", "message": "missing"}},
    )
    strategy_registry = create_default_strategy_registry()

    updated_exec = verify_task(execution, "task_v3", store, strategy_registry)
    task = updated_exec.tasks["task_v3"]

    assert task.status == TaskStatus.FAILED
    assert task.verification.status == VerificationStatus.VERIFIED_FAILURE

    event_types = [e.event_type for e in store.get_events("exec_v3")]
    assert EventType.VERIFICATION_FAILED in event_types
    assert EventType.TASK_COMPLETED not in event_types
    assert EventType.EXECUTION_COMPLETED not in event_types


# ---------------------------------------------------------------------------
# 7. verify_task unknown outcome
# ---------------------------------------------------------------------------

def test_verify_task_unknown_outcome():
    store = InMemoryExecutionStore()
    execution = _make_verifying_task(
        store, "exec_v4", "task_v4",
        result={"success": False, "status": "unknown", "error": {"type": "TimeoutError", "message": "timed out"}},
    )
    strategy_registry = create_default_strategy_registry()

    updated_exec = verify_task(execution, "task_v4", store, strategy_registry)
    task = updated_exec.tasks["task_v4"]

    assert task.status == TaskStatus.FAILED
    assert task.verification.status == VerificationStatus.UNKNOWN

    # is_unknown must be flagged in the VERIFICATION_FAILED event
    events = store.get_events("exec_v4")
    failed_events = [e for e in events if e.event_type == EventType.VERIFICATION_FAILED]
    assert len(failed_events) == 1
    assert failed_events[0].metadata.get("is_unknown") is True

    # last_error must be set with unknown information
    assert updated_exec.last_error is not None
    assert updated_exec.last_error.get("type") == "UnknownVerificationOutcome"


# ---------------------------------------------------------------------------
# 8. verify_task rejects non-VERIFYING tasks
# ---------------------------------------------------------------------------

def test_verify_task_rejects_non_verifying_status():
    execution = Execution(execution_id="exec_v5", objective="Test")
    task = Task(
        task_id="task_v5",
        execution_id="exec_v5",
        title="Ready task",
        description="Not yet verifying",
        tool_name="mock_tool",
        status=TaskStatus.READY,
    )
    execution.add_task(task)
    store = _make_store_with_exec(execution)
    strategy_registry = create_default_strategy_registry()

    with pytest.raises(TaskNotInVerifyingStateError, match="expected 'VERIFYING'"):
        verify_task(execution, "task_v5", store, strategy_registry)


def test_verify_task_rejects_succeeded_status():
    execution = Execution(execution_id="exec_v6", objective="Test")
    task = Task(
        task_id="task_v6",
        execution_id="exec_v6",
        title="Already done",
        description="Already succeeded",
        tool_name="mock_tool",
        status=TaskStatus.VERIFYING,
    )
    # manually push to SUCCEEDED to bypass transition guard
    task.status = TaskStatus.SUCCEEDED
    execution.add_task(task)
    store = _make_store_with_exec(execution)
    strategy_registry = create_default_strategy_registry()

    with pytest.raises(TaskNotInVerifyingStateError):
        verify_task(execution, "task_v6", store, strategy_registry)


# ---------------------------------------------------------------------------
# 9. verify_task rejects missing task
# ---------------------------------------------------------------------------

def test_verify_task_rejects_missing_task():
    execution = Execution(execution_id="exec_v7", objective="Test")
    store = _make_store_with_exec(execution)
    strategy_registry = create_default_strategy_registry()

    with pytest.raises(TaskNotFoundError, match="not found"):
        verify_task(execution, "nonexistent_task", store, strategy_registry)


# ---------------------------------------------------------------------------
# 10. Full execute + verify pipeline
# ---------------------------------------------------------------------------

def test_full_execute_then_verify_pipeline():
    """
    Validates that executor + verifier work correctly in sequence.
    Executor stops at VERIFYING; verifier completes to SUCCEEDED.
    """
    store = InMemoryExecutionStore()
    tool_registry = create_default_tool_registry()
    strategy_registry = create_default_strategy_registry()

    execution = Execution(execution_id="exec_pipe", objective="Full pipeline")
    task = Task(
        task_id="task_pipe",
        execution_id="exec_pipe",
        title="Inspect workspace",
        description="Run and verify workspace inspection",
        tool_name="inspect_project_workspace",
        tool_input={"directory_path": "."},
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    # Phase 1: executor
    after_exec = execute_task(execution, "task_pipe", store, tool_registry)
    assert after_exec.tasks["task_pipe"].status == TaskStatus.VERIFYING
    assert after_exec.tasks["task_pipe"].verification is None
    assert after_exec.status != ExecutionStatus.COMPLETED

    # Phase 2: verifier
    after_verify = verify_task(after_exec, "task_pipe", store, strategy_registry)
    task_res = after_verify.tasks["task_pipe"]

    assert task_res.status == TaskStatus.SUCCEEDED
    assert task_res.verification is not None
    assert task_res.verification.status == VerificationStatus.VERIFIED_SUCCESS
    assert after_verify.status == ExecutionStatus.COMPLETED

    # Complete audit trail
    event_types = [e.event_type for e in store.get_events("exec_pipe")]
    assert EventType.TASK_STARTED in event_types
    assert EventType.VERIFICATION_STARTED in event_types
    assert EventType.VERIFICATION_SUCCEEDED in event_types
    assert EventType.TASK_COMPLETED in event_types
    assert EventType.EXECUTION_COMPLETED in event_types


# ---------------------------------------------------------------------------
# 11. Multi-task: second task unlocked after first verified
# ---------------------------------------------------------------------------

def test_multi_task_second_unlocked_after_verification():
    """
    task_b depends on task_a.
    After task_a is verified (SUCCEEDED), task_b must become READY.
    """
    store = InMemoryExecutionStore()
    tool_registry = create_default_tool_registry()
    strategy_registry = create_default_strategy_registry()

    execution = Execution(execution_id="exec_multi", objective="Multi-task")
    task_a = Task(
        task_id="task_a",
        execution_id="exec_multi",
        title="Task A",
        description="First",
        tool_name="inspect_project_workspace",
        tool_input={"directory_path": "."},
    )
    task_b = Task(
        task_id="task_b",
        execution_id="exec_multi",
        title="Task B",
        description="Depends on A",
        tool_name="inspect_project_workspace",
        tool_input={"directory_path": "."},
        dependencies=["task_a"],
    )
    execution.add_task(task_a)
    execution.add_task(task_b)
    execution.update_task_statuses()
    store.create_execution(execution)

    assert execution.tasks["task_a"].status == TaskStatus.READY
    assert execution.tasks["task_b"].status == TaskStatus.PENDING

    # Execute task_a
    after_exec_a = execute_task(execution, "task_a", store, tool_registry)
    assert after_exec_a.tasks["task_a"].status == TaskStatus.VERIFYING
    # task_b still cannot run — task_a not yet SUCCEEDED
    assert after_exec_a.tasks["task_b"].status == TaskStatus.PENDING

    # Verify task_a
    after_verify_a = verify_task(after_exec_a, "task_a", store, strategy_registry)
    assert after_verify_a.tasks["task_a"].status == TaskStatus.SUCCEEDED
    # task_b should now be READY (update_task_statuses was called in verifier)
    assert after_verify_a.tasks["task_b"].status == TaskStatus.READY

    # Execution is not COMPLETED yet (task_b still pending)
    assert after_verify_a.status != ExecutionStatus.COMPLETED


# ---------------------------------------------------------------------------
# 12. Checkpoint after verification
# ---------------------------------------------------------------------------

def test_verify_task_creates_after_verification_checkpoint():
    store = InMemoryExecutionStore()
    execution = _make_verifying_task(
        store, "exec_v8", "task_v8",
        result={"success": True, "directory": ".", "files": []},
        tool_name="inspect_project_workspace",
    )
    strategy_registry = create_default_strategy_registry()

    verify_task(execution, "task_v8", store, strategy_registry)

    checkpoints = store.get_checkpoints("exec_v8")
    reasons = [c.reason for c in checkpoints]
    assert "after_verification" in reasons


# ---------------------------------------------------------------------------
# 13. No false positive: custom strategy can block even when tool says True
# ---------------------------------------------------------------------------

def test_custom_strategy_can_reject_tool_success():
    """
    A registered strategy may inspect deeper evidence and return VERIFIED_FAILURE
    even when the raw tool result has success: True. This prevents the verifier
    from being trivially fooled by a tool that incorrectly reports success.
    """
    store = InMemoryExecutionStore()
    execution = _make_verifying_task(
        store, "exec_v9", "task_v9",
        result={"success": True, "data": {"count": 0}},  # tool says ok
        tool_name="strict_tool",
    )

    strategy_registry = VerificationStrategyRegistry()

    def strict_strategy(task: Task, exec_: Execution) -> VerificationResult:
        """Rejects result if count == 0 even though tool reported success."""
        count = (task.result or {}).get("data", {}).get("count", -1)
        if count == 0:
            return VerificationResult(
                status=VerificationStatus.VERIFIED_FAILURE,
                message="count must be > 0",
                evidence={"count": count},
            )
        return VerificationResult(
            status=VerificationStatus.VERIFIED_SUCCESS,
            message="count is valid",
            evidence={"count": count},
        )

    strategy_registry.register("strict_tool", strict_strategy)

    updated_exec = verify_task(execution, "task_v9", store, strategy_registry)
    task = updated_exec.tasks["task_v9"]

    assert task.status == TaskStatus.FAILED
    assert task.verification.status == VerificationStatus.VERIFIED_FAILURE
    assert "count must be > 0" in task.verification.message
