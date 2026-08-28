"""
Tests for Phase 2.5 — Recovery Engine

Coverage:
  1. Transient failure -> RETRY (respects max_attempts)
  2. Respects max_attempts -> FAIL when exhausted
  3. Permanent failure -> FAIL immediately (no retry)
  4. UNKNOWN + external FOUND -> SUCCEEDED (idempotent, no blind retry)
  5. UNKNOWN + external NOT_FOUND -> RETRY
  6. UNKNOWN + external UNKNOWN -> ESCALATE -> FAIL
  7. Recovery history persisted
  8. Checkpoints before_recovery / after_recovery
  9. Events RECOVERY_STARTED / RECOVERY_SELECTED / RETRY_STARTED
 10. Preserves operation_id across recovery
 11. Rejects SUCCEEDED task
 12. Rejects invalid task state / missing task
 13. Does not execute tool (recovery never calls tool)
"""
from __future__ import annotations

from typing import Any

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.executor import TaskNotFoundError, execute_task
from app.orchestration.recovery import (
    FailureCategory,
    RecoveryError,
    TaskNotFailedError,
    classify_failure,
    recover_task,
)
from app.orchestration.state import (
    EventType,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from app.orchestration.verifier import create_default_strategy_registry, verify_task
from app.tools.registry import ToolRegistry, create_default_tool_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_failed_execution(
    exec_id: str,
    task_id: str,
    attempt_count: int = 1,
    max_attempts: int = 3,
    verification_status: VerificationStatus = VerificationStatus.VERIFIED_FAILURE,
    error_type: str = "RuntimeError",
    error_message: str = "transient boom",
    execution_status: ExecutionStatus = ExecutionStatus.VERIFYING,
) -> tuple[InMemoryExecutionStore, Execution]:
    """Creates an execution with a single task in FAILED state (post-verifier)."""
    store = InMemoryExecutionStore()
    execution = Execution(execution_id=exec_id, objective="Recovery test")
    task = Task(
        task_id=task_id,
        execution_id=exec_id,
        title="Failing task",
        description="Will be recovered",
        tool_name="mock_tool",
        status=TaskStatus.FAILED,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )
    task.error = {"type": error_type, "message": error_message}
    task.result = {"success": False, "error": task.error}
    if verification_status == VerificationStatus.UNKNOWN:
        task.verification = VerificationResult(
            status=VerificationStatus.UNKNOWN,
            message="unknown outcome",
            evidence={"task_id": task_id, "error_type": error_type},
        )
    elif verification_status == VerificationStatus.VERIFIED_FAILURE:
        task.verification = VerificationResult(
            status=VerificationStatus.VERIFIED_FAILURE,
            message="verified failure",
            evidence={"error": task.error, "task_id": task_id},
        )
    else:
        task.verification = VerificationResult(
            status=verification_status,
            message="success",
            evidence={"task_id": task_id},
        )
    # Ensure execution can transition to RECOVERING
    execution.add_task(task)
    # Force execution status for recovery
    execution.status = execution_status
    # If still PENDING, move through valid transitions
    if execution_status == ExecutionStatus.VERIFYING:
        # PENDING -> PLANNING -> EXECUTING -> VERIFYING
        # Use direct assignment for test setup, bypassing transition checks where needed
        # But we can transition properly:
        try:
            execution.status = ExecutionStatus.PENDING
            execution.transition_to(ExecutionStatus.PLANNING)
            execution.transition_to(ExecutionStatus.EXECUTING)
            execution.transition_to(ExecutionStatus.VERIFYING)
        except Exception:
            execution.status = execution_status
    store.create_execution(execution)
    # Task is already FAILED, ensure stored
    stored = store.get_execution(exec_id)
    # Ensure task status is FAILED in store (transition may have changed)
    stored.tasks[task_id].status = TaskStatus.FAILED
    stored.tasks[task_id].verification = task.verification
    stored.tasks[task_id].error = task.error
    stored.tasks[task_id].result = task.result
    stored.tasks[task_id].attempt_count = attempt_count
    stored.tasks[task_id].max_attempts = max_attempts
    store.update_execution(stored)
    return store, store.get_execution(exec_id)


def _make_unknown_failed_execution(
    exec_id: str,
    task_id: str,
    attempt_count: int = 1,
    max_attempts: int = 3,
) -> tuple[InMemoryExecutionStore, Execution]:
    return _make_failed_execution(
        exec_id, task_id,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        verification_status=VerificationStatus.UNKNOWN,
        error_type="TimeoutError",
        error_message="timed out with unknown state",
    )


# ---------------------------------------------------------------------------
# 1. Transient retry
# ---------------------------------------------------------------------------

def test_recovery_retries_transient_failure():
    store, exec_before = _make_failed_execution("exec_rec1", "task_rec1", attempt_count=1, max_attempts=3, error_type="RuntimeError")
    op_id = exec_before.tasks["task_rec1"].operation_id

    after = recover_task(exec_before, "task_rec1", store)

    task = after.tasks["task_rec1"]
    assert task.status == TaskStatus.READY
    assert task.operation_id == op_id  # idempotency
    assert after.status == ExecutionStatus.EXECUTING
    assert len(after.recovery_history) == 1
    assert after.recovery_history[0]["recovery_action"] == "RETRY"
    assert after.recovery_history[0]["reason"] == FailureCategory.TRANSIENT.value

    events = store.get_events("exec_rec1")
    types = [e.event_type for e in events]
    assert EventType.RECOVERY_STARTED in types
    assert EventType.RECOVERY_SELECTED in types
    assert EventType.RETRY_STARTED in types


def test_recovery_respects_max_attempts():
    store, exec_before = _make_failed_execution("exec_rec2", "task_rec2", attempt_count=3, max_attempts=3, error_type="RuntimeError")

    after = recover_task(exec_before, "task_rec2", store)

    assert after.tasks["task_rec2"].status == TaskStatus.FAILED
    assert after.status == ExecutionStatus.FAILED
    assert after.recovery_history[0]["recovery_action"] == "FAIL"
    events = store.get_events("exec_rec2")
    assert EventType.RETRY_STARTED not in [e.event_type for e in events]
    assert EventType.EXECUTION_FAILED in [e.event_type for e in events]


def test_recovery_permanent_failure():
    """Permanent errors should FAIL immediately even if attempts remain."""
    store, exec_before = _make_failed_execution("exec_rec3", "task_rec3", attempt_count=1, max_attempts=3, error_type="ValidationError")

    after = recover_task(exec_before, "task_rec3", store)

    assert after.tasks["task_rec3"].status == TaskStatus.FAILED
    assert after.status == ExecutionStatus.FAILED
    assert after.recovery_history[0]["reason"] == FailureCategory.PERMANENT.value
    assert after.recovery_history[0]["recovery_action"] == "FAIL"


# ---------------------------------------------------------------------------
# Unknown handling
# ---------------------------------------------------------------------------

def test_recovery_unknown_found():
    store, exec_before = _make_unknown_failed_execution("exec_rec4", "task_rec4", attempt_count=1, max_attempts=3)

    def checker(task: Task) -> str:
        return "FOUND"

    after = recover_task(exec_before, "task_rec4", store, external_state_checker=checker)

    task = after.tasks["task_rec4"]
    assert task.status == TaskStatus.SUCCEEDED
    assert task.verification is not None
    assert task.verification.status == VerificationStatus.VERIFIED_SUCCESS
    # Single-task execution should be COMPLETED after FOUND
    assert after.status == ExecutionStatus.COMPLETED
    assert after.recovery_history[0]["recovery_action"] == "FOUND_SUCCESS"
    assert after.recovery_history[0]["external_state"] == "FOUND"


def test_recovery_unknown_not_found():
    store, exec_before = _make_unknown_failed_execution("exec_rec5", "task_rec5", attempt_count=1, max_attempts=3)

    def checker(task: Task) -> str:
        return "NOT_FOUND"

    after = recover_task(exec_before, "task_rec5", store, external_state_checker=checker)

    assert after.tasks["task_rec5"].status == TaskStatus.READY
    assert after.status == ExecutionStatus.EXECUTING
    assert after.recovery_history[0]["recovery_action"] == "RETRY"
    assert after.recovery_history[0]["external_state"] == "NOT_FOUND"


def test_recovery_unknown_still_unknown():
    store, exec_before = _make_unknown_failed_execution("exec_rec6", "task_rec6", attempt_count=1, max_attempts=3)

    def checker(task: Task) -> str:
        return "UNKNOWN"

    after = recover_task(exec_before, "task_rec6", store, external_state_checker=checker)

    assert after.tasks["task_rec6"].status == TaskStatus.FAILED
    assert after.status == ExecutionStatus.FAILED
    assert after.recovery_history[0]["recovery_action"] == "ESCALATE"
    assert after.recovery_history[0]["external_state"] == "UNKNOWN"
    events = store.get_events("exec_rec6")
    assert EventType.EXECUTION_FAILED in [e.event_type for e in events]


# ---------------------------------------------------------------------------
# History, checkpoints, events, operation_id
# ---------------------------------------------------------------------------

def test_recovery_history_persisted():
    store, exec_before = _make_failed_execution("exec_rec7", "task_rec7")
    after = recover_task(exec_before, "task_rec7", store)

    assert len(after.recovery_history) == 1
    entry = after.recovery_history[0]
    assert entry["task_id"] == "task_rec7"
    assert entry["previous_status"] == TaskStatus.FAILED.value
    assert "timestamp" in entry
    assert "operation_id" in entry
    assert entry["attempt"] == 1

    # Second recovery after a retry fails again should append
    # Simulate retry execution failing again
    # Task is now READY -> run executor mock failure again
    # For test, manually set back to FAILED with incremented attempt
    after.tasks["task_rec7"].status = TaskStatus.FAILED
    after.tasks["task_rec7"].attempt_count = 2
    after.status = ExecutionStatus.VERIFYING
    store.update_execution(after)
    after2 = recover_task(after, "task_rec7", store)
    assert len(after2.recovery_history) == 2


def test_recovery_checkpoints_created():
    store, exec_before = _make_failed_execution("exec_rec8", "task_rec8")
    recover_task(exec_before, "task_rec8", store)

    checkpoints = store.get_checkpoints("exec_rec8")
    reasons = [c.reason for c in checkpoints]
    assert "before_recovery" in reasons
    assert "after_recovery" in reasons


def test_recovery_events_created():
    store, exec_before = _make_failed_execution("exec_rec9", "task_rec9")
    recover_task(exec_before, "task_rec9", store)

    events = store.get_events("exec_rec9")
    types = [e.event_type for e in events]
    assert EventType.RECOVERY_STARTED in types
    assert EventType.RECOVERY_SELECTED in types
    assert EventType.CHECKPOINT_CREATED in types
    # Check metadata
    rec_started = [e for e in events if e.event_type == EventType.RECOVERY_STARTED][0]
    assert rec_started.actor == "recovery"
    assert "category" in rec_started.metadata


def test_recovery_preserves_operation_id():
    store, exec_before = _make_failed_execution("exec_rec10", "task_rec10")
    op_id_before = exec_before.tasks["task_rec10"].operation_id

    after = recover_task(exec_before, "task_rec10", store)
    assert after.tasks["task_rec10"].operation_id == op_id_before

    # Even on FOUND success, operation_id preserved
    store2, exec2 = _make_unknown_failed_execution("exec_rec10b", "task_rec10b")
    op2 = exec2.tasks["task_rec10b"].operation_id

    def checker(task: Task) -> str:
        return "FOUND"

    after2 = recover_task(exec2, "task_rec10b", store2, external_state_checker=checker)
    assert after2.tasks["task_rec10b"].operation_id == op2


# ---------------------------------------------------------------------------
# Rejects invalid states
# ---------------------------------------------------------------------------

def test_recovery_rejects_succeeded_task():
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_rec11", objective="Test")
    task = Task(task_id="task_rec11", execution_id="exec_rec11", title="Done", description="Already succeeded", status=TaskStatus.SUCCEEDED)
    execution.add_task(task)
    execution.status = ExecutionStatus.VERIFYING
    store.create_execution(execution)
    stored = store.get_execution("exec_rec11")
    stored.tasks["task_rec11"].status = TaskStatus.SUCCEEDED
    store.update_execution(stored)

    with pytest.raises(TaskNotFailedError, match="expected 'FAILED'"):
        recover_task(stored, "task_rec11", store)


def test_recovery_rejects_invalid_state():
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_rec12", objective="Test")
    task = Task(task_id="task_rec12", execution_id="exec_rec12", title="Ready", description="Not failed", status=TaskStatus.READY)
    execution.add_task(task)
    execution.status = ExecutionStatus.EXECUTING
    store.create_execution(execution)

    with pytest.raises(TaskNotFailedError):
        recover_task(execution, "task_rec12", store)

    # Missing task
    with pytest.raises(TaskNotFoundError, match="not found"):
        recover_task(execution, "nonexistent", store)


def test_recovery_does_not_execute_tool():
    """Recovery must never call the tool; operation_id stable and tool call count 0."""
    store, exec_before = _make_failed_execution("exec_rec13", "task_rec13")
    # Register a tool that would fail the test if called
    registry = ToolRegistry()
    calls: list[str] = []

    def should_not_be_called(operation_id: str = None):
        calls.append("called")
        return {"success": True}

    registry.register("should_not_be_called", should_not_be_called)
    # Task uses that tool name, but recovery should not invoke it
    exec_before.tasks["task_rec13"].tool_name = "should_not_be_called"
    store.update_execution(store.get_execution("exec_rec13"))
    # Need to re-fetch with updated tool_name
    stored = store.get_execution("exec_rec13")
    stored.tasks["task_rec13"].tool_name = "should_not_be_called"
    stored.tasks["task_rec13"].status = TaskStatus.FAILED
    store.update_execution(stored)
    refreshed = store.get_execution("exec_rec13")

    after = recover_task(refreshed, "task_rec13", store)

    assert calls == []
    # Recovery only decided RETRY, did not execute
    assert after.tasks["task_rec13"].status == TaskStatus.READY
    # Executor is responsible for actual tool call
    tool_calls_before = len(calls)
    # Now executor should be able to run the tool
    after_exec = execute_task(after, "task_rec13", store, registry)
    assert len(calls) == tool_calls_before + 1
    assert after_exec.tasks["task_rec13"].status == TaskStatus.VERIFYING


# ---------------------------------------------------------------------------
# Deterministic precondition failures (NoTestSuiteFound) — never retried
# ---------------------------------------------------------------------------

def test_classify_no_test_suite_found_is_permanent_via_task_error():
    """Executor-direct path: task.error carries NoTestSuiteFound, verification None."""
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_nts1", objective="Run tests")
    task = Task(
        task_id="t_nts1", execution_id="exec_nts1", title="T", description="D",
        tool_name="run_test_suite", status=TaskStatus.FAILED, attempt_count=1, max_attempts=3,
    )
    task.error = {"type": "NoTestSuiteFound", "message": "No test suite found at 'tests'"}
    task.result = {"success": False, "error": task.error}
    execution.add_task(task)
    execution.status = ExecutionStatus.EXECUTING
    store.create_execution(execution)
    stored = store.get_execution("exec_nts1")
    stored.tasks["t_nts1"].status = TaskStatus.FAILED

    assert classify_failure(stored.tasks["t_nts1"]) == FailureCategory.PERMANENT


def test_classify_no_test_suite_found_is_permanent_via_result_error():
    """Result-carried error type must also classify as PERMANENT."""
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_nts2", objective="Run tests")
    task = Task(
        task_id="t_nts2", execution_id="exec_nts2", title="T", description="D",
        tool_name="run_test_suite", status=TaskStatus.FAILED, attempt_count=1, max_attempts=3,
    )
    execution.add_task(task)
    execution.status = ExecutionStatus.EXECUTING
    store.create_execution(execution)
    stored = store.get_execution("exec_nts2")
    stored.tasks["t_nts2"].status = TaskStatus.FAILED
    stored.tasks["t_nts2"].result = {
        "success": False,
        "error": {"type": "NoTestSuiteFound", "message": "no suite"},
    }
    store.update_execution(stored)

    assert classify_failure(stored.tasks["t_nts2"]) == FailureCategory.PERMANENT


def test_classify_verifier_no_suite_evidence_is_permanent():
    """Verifier evidence with explicit_outcome NO_TEST_SUITE_FOUND classifies PERMANENT."""
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_nts3", objective="Run tests")
    task = Task(
        task_id="t_nts3", execution_id="exec_nts3", title="T", description="D",
        tool_name="run_test_suite", status=TaskStatus.FAILED, attempt_count=1, max_attempts=3,
    )
    execution.add_task(task)
    execution.status = ExecutionStatus.VERIFYING
    store.create_execution(execution)
    stored = store.get_execution("exec_nts3")
    stored.tasks["t_nts3"].status = TaskStatus.FAILED
    from app.orchestration.state import VerificationResult as VR

    stored.tasks["t_nts3"].verification = VR(
        status=VerificationStatus.VERIFIED_FAILURE,
        message="Test suite not available",
        evidence={"explicit_outcome": "NO_TEST_SUITE_FOUND", "error_type": "NoTestSuiteFound"},
    )
    store.update_execution(stored)

    assert classify_failure(stored.tasks["t_nts3"]) == FailureCategory.PERMANENT


def test_recover_no_test_suite_does_not_retry():
    """Full recovery: NoTestSuiteFound fails immediately — no RETRY_STARTED, single attempt."""
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_nts4", objective="Run my test suite and report failures")
    task = Task(
        task_id="t_nts4", execution_id="exec_nts4", title="Run tests", description="D",
        tool_name="run_test_suite", status=TaskStatus.FAILED, attempt_count=1, max_attempts=3,
    )
    task.error = {"type": "NoTestSuiteFound", "message": "path does not exist"}
    task.result = {"success": False, "status": "no_test_suite", "error": task.error}
    execution.add_task(task)
    execution.status = ExecutionStatus.EXECUTING
    store.create_execution(execution)
    stored = store.get_execution("exec_nts4")
    stored.tasks["t_nts4"].status = TaskStatus.FAILED
    stored.tasks["t_nts4"].error = task.error
    stored.tasks["t_nts4"].result = task.result
    store.update_execution(stored)

    after = recover_task(stored, "t_nts4", store)

    # PERMANENT -> FAIL immediately: stays FAILED, never READY for retry
    assert after.tasks["t_nts4"].status == TaskStatus.FAILED
    assert after.status == ExecutionStatus.FAILED
    assert after.tasks["t_nts4"].attempt_count == 1  # executed once only
    assert after.last_error["category"] == "PERMANENT"
    assert after.last_error["recovery_action"] == "FAIL"

    event_types = [e.event_type.value for e in store.get_events("exec_nts4")]
    assert "RECOVERY_STARTED" in event_types
    assert "RECOVERY_SELECTED" in event_types
    assert "RETRY_STARTED" not in event_types  # THE assertion: no retry scheduled


def test_transient_failures_still_retry_after_classification_change():
    """Guard: genuine transient errors keep TRANSIENT classification and retry."""
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_trans_guard", objective="Run tests")
    task = Task(
        task_id="t_trans", execution_id="exec_trans_guard", title="T", description="D",
        tool_name="run_test_suite", status=TaskStatus.FAILED, attempt_count=1, max_attempts=3,
    )
    task.error = {"type": "RuntimeError", "message": "temporary network blip"}
    task.result = {"success": False, "error": task.error}
    execution.add_task(task)
    execution.status = ExecutionStatus.EXECUTING
    store.create_execution(execution)
    stored = store.get_execution("exec_trans_guard")
    stored.tasks["t_trans"].status = TaskStatus.FAILED
    stored.tasks["t_trans"].error = task.error
    stored.tasks["t_trans"].result = task.result
    store.update_execution(stored)

    assert classify_failure(stored.tasks["t_trans"]) == FailureCategory.TRANSIENT

    after = recover_task(stored, "t_trans", store)
    assert after.tasks["t_trans"].status == TaskStatus.READY  # retry scheduled
    event_types = [e.event_type.value for e in store.get_events("exec_trans_guard")]
    assert "RETRY_STARTED" in event_types
