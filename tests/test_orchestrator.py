"""
Tests for Phase 2.6 — Orchestration Controller

Verifies that the orchestrator correctly coordinates
executor → verifier → recovery without bypassing any layer.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.orchestrator import ExecutionOrchestrator
from app.orchestration.state import (
    EventType,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from app.orchestration.verifier import VerificationStrategyRegistry, create_default_strategy_registry
from app.tools.registry import ToolRegistry, create_default_tool_registry


def _make_orchestrator(
    store: InMemoryExecutionStore,
    tool_registry: ToolRegistry | None = None,
    strategy_registry: VerificationStrategyRegistry | None = None,
    external_state_checker=None,
):
    return ExecutionOrchestrator(
        store=store,
        tool_registry=tool_registry or create_default_tool_registry(),
        strategy_registry=strategy_registry or create_default_strategy_registry(),
        external_state_checker=external_state_checker,
    )


# ---------------------------------------------------------------------------
# 1. Single task success
# ---------------------------------------------------------------------------

def test_orchestrator_single_task_success():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()
    strategy_registry = create_default_strategy_registry()

    execution = Execution(execution_id="exec_orch1", objective="Single task")
    task = Task(
        task_id="t1",
        execution_id="exec_orch1",
        title="Inspect",
        description="Inspect workspace",
        tool_name="inspect_project_workspace",
        tool_input={"directory_path": "."},
    )
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, strategy_registry)
    result = orchestrator.run("exec_orch1")

    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["t1"].status == TaskStatus.SUCCEEDED
    assert result.tasks["t1"].verification is not None
    assert result.tasks["t1"].verification.status == VerificationStatus.VERIFIED_SUCCESS


# ---------------------------------------------------------------------------
# 2. Multiple tasks dependency order
# ---------------------------------------------------------------------------

def test_orchestrator_multiple_tasks_dependency_order():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()
    strategy_registry = create_default_strategy_registry()

    execution = Execution(execution_id="exec_orch2", objective="Multi")
    task_a = Task(task_id="task_a", execution_id="exec_orch2", title="A", description="A", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
    task_b = Task(task_id="task_b", execution_id="exec_orch2", title="B", description="B depends on A", tool_name="inspect_project_workspace", tool_input={"directory_path": "."}, dependencies=["task_a"])
    task_c = Task(task_id="task_c", execution_id="exec_orch2", title="C", description="C depends on B", tool_name="inspect_project_workspace", tool_input={"directory_path": "."}, dependencies=["task_b"])
    execution.add_task(task_a)
    execution.add_task(task_b)
    execution.add_task(task_c)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, strategy_registry)
    result = orchestrator.run("exec_orch2")

    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["task_a"].status == TaskStatus.SUCCEEDED
    assert result.tasks["task_b"].status == TaskStatus.SUCCEEDED
    assert result.tasks["task_c"].status == TaskStatus.SUCCEEDED

    events = store.get_events("exec_orch2")
    # Ensure ordering: task_a completed before task_b started
    task_started = [e for e in events if e.event_type == EventType.TASK_STARTED]
    assert [e.task_id for e in task_started] == ["task_a", "task_b", "task_c"]


# ---------------------------------------------------------------------------
# 3. Verification failure leads to recovery (verifier rejects tool success)
# ---------------------------------------------------------------------------

def test_orchestrator_verification_failure():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def always_true_tool():
        return {"success": True, "data": {"count": 0}}

    registry.register("strict_tool", always_true_tool)

    strategy_registry = VerificationStrategyRegistry()

    def strict_strategy(task: Task, execution: Execution) -> VerificationResult:
        count = (task.result or {}).get("data", {}).get("count", -1)
        if count == 0:
            return VerificationResult(status=VerificationStatus.VERIFIED_FAILURE, message="count must be >0", evidence={"count": count})
        return VerificationResult(status=VerificationStatus.VERIFIED_SUCCESS, message="ok", evidence={})

    strategy_registry.register("strict_tool", strict_strategy)

    execution = Execution(execution_id="exec_orch3", objective="Verify fail")
    task = Task(task_id="t_verify_fail", execution_id="exec_orch3", title="Strict", description="Will be rejected by verifier", tool_name="strict_tool")
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, strategy_registry)
    result = orchestrator.run("exec_orch3")

    # Tool succeeded but verifier failed; recovery should treat as TRANSIENT and retry until max_attempts
    # After 3 attempts, should be FAILED
    assert result.status == ExecutionStatus.FAILED
    assert result.tasks["t_verify_fail"].status == TaskStatus.FAILED
    assert result.tasks["t_verify_fail"].attempt_count == 3


# ---------------------------------------------------------------------------
# 4. Transient failure retry then success
# ---------------------------------------------------------------------------

def test_orchestrator_transient_failure_retry():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()
    calls: list[str] = []

    def flaky_tool(operation_id: str = None):
        calls.append(operation_id)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"success": True, "data": {"ok": True}}

    registry.register("flaky_tool", flaky_tool)

    execution = Execution(execution_id="exec_orch4", objective="Flaky")
    task = Task(task_id="t_flaky", execution_id="exec_orch4", title="Flaky", description="Fails once", tool_name="flaky_tool")
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run("exec_orch4")

    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["t_flaky"].status == TaskStatus.SUCCEEDED
    assert result.tasks["t_flaky"].attempt_count == 2
    assert len(calls) == 2
    assert calls[0] == calls[1]  # operation_id preserved
    assert len(result.recovery_history) == 1
    assert result.recovery_history[0]["recovery_action"] == "RETRY"


# ---------------------------------------------------------------------------
# 5. Unknown outcome recovery (timeout) with external checker
# ---------------------------------------------------------------------------

def test_orchestrator_unknown_outcome_recovery():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def timeout_tool():
        raise TimeoutError("timed out unknown")

    registry.register("timeout_tool", timeout_tool)

    execution = Execution(execution_id="exec_orch5", objective="Unknown")
    task = Task(task_id="t_timeout", execution_id="exec_orch5", title="Timeout", description="Unknown state", tool_name="timeout_tool")
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    def checker(task: Task) -> str:
        return "NOT_FOUND"  # safe to retry

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry(), external_state_checker=checker)
    # First attempt will timeout -> UNKNOWN -> NOT_FOUND -> RETRY
    # But second attempt also timeout, will retry again, third attempt timeout -> max attempts -> FAIL
    result = orchestrator.run("exec_orch5")
    # With NOT_FOUND checker, each UNKNOWN will be retried until max_attempts=3
    assert result.status == ExecutionStatus.FAILED
    assert result.tasks["t_timeout"].attempt_count == 3

    # Now test FOUND -> immediate success without retry
    store2 = InMemoryExecutionStore()
    registry2 = ToolRegistry()
    registry2.register("timeout_tool2", timeout_tool)
    execution2 = Execution(execution_id="exec_orch5b", objective="Unknown FOUND")
    task2 = Task(task_id="t_timeout2", execution_id="exec_orch5b", title="Timeout2", description="Unknown FOUND", tool_name="timeout_tool2")
    execution2.add_task(task2)
    execution2.update_task_statuses()
    store2.create_execution(execution2)

    def checker_found(task: Task) -> str:
        return "FOUND"

    orchestrator2 = _make_orchestrator(store2, registry2, create_default_strategy_registry(), external_state_checker=checker_found)
    result2 = orchestrator2.run("exec_orch5b")
    assert result2.status == ExecutionStatus.COMPLETED
    assert result2.tasks["t_timeout2"].status == TaskStatus.SUCCEEDED
    assert result2.tasks["t_timeout2"].attempt_count == 1  # no retry needed


# ---------------------------------------------------------------------------
# 6. Permanent failure stops immediately
# ---------------------------------------------------------------------------

def test_orchestrator_permanent_failure():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def bad_tool():
        return {"success": False, "error": {"type": "ValidationError", "message": "bad input"}}

    registry.register("bad_tool", bad_tool)

    execution = Execution(execution_id="exec_orch6", objective="Permanent")
    task = Task(task_id="t_perm", execution_id="exec_orch6", title="Permanent", description="Validation error", tool_name="bad_tool")
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run("exec_orch6")

    assert result.status == ExecutionStatus.FAILED
    assert result.tasks["t_perm"].status == TaskStatus.FAILED
    assert result.tasks["t_perm"].attempt_count == 1  # no retry for permanent
    assert len(result.recovery_history) == 1
    assert result.recovery_history[0]["reason"] == "PERMANENT"


# ---------------------------------------------------------------------------
# 7. Respects max_attempts
# ---------------------------------------------------------------------------

def test_orchestrator_respects_max_attempts():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def always_fail():
        raise RuntimeError("always fail")

    registry.register("always_fail", always_fail)

    execution = Execution(execution_id="exec_orch7", objective="Max attempts")
    task = Task(task_id="t_max", execution_id="exec_orch7", title="Max", description="Always fail", tool_name="always_fail", max_attempts=2)
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run("exec_orch7")

    assert result.tasks["t_max"].attempt_count == 2
    assert result.status == ExecutionStatus.FAILED
    assert len(result.recovery_history) == 2  # two recoveries, second fails permanently


# ---------------------------------------------------------------------------
# 8. Completes all tasks
# ---------------------------------------------------------------------------

def test_orchestrator_completes_all_tasks():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()
    execution = Execution(execution_id="exec_orch8", objective="Complete all")
    for i in range(3):
        t = Task(task_id=f"t_{i}", execution_id="exec_orch8", title=f"T{i}", description=f"Task {i}", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
        execution.add_task(t)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run("exec_orch8")
    assert result.status == ExecutionStatus.COMPLETED
    assert all(t.status == TaskStatus.SUCCEEDED for t in result.tasks.values())


# ---------------------------------------------------------------------------
# 9. Stops on unrecoverable failure (blocks dependents)
# ---------------------------------------------------------------------------

def test_orchestrator_stops_on_unrecoverable_failure():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def fail_tool():
        return {"success": False, "error": {"type": "ValidationError", "message": "perm"}}

    registry.register("fail_perm", fail_tool)
    registry.register("ok_tool", lambda: {"success": True, "data": {}})

    execution = Execution(execution_id="exec_orch9", objective="Stop on fail")
    task_a = Task(task_id="task_a", execution_id="exec_orch9", title="A", description="Permanent fail", tool_name="fail_perm")
    task_b = Task(task_id="task_b", execution_id="exec_orch9", title="B", description="Depends on A", tool_name="ok_tool", dependencies=["task_a"])
    execution.add_task(task_a)
    execution.add_task(task_b)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run("exec_orch9")

    assert result.status == ExecutionStatus.FAILED
    assert result.tasks["task_a"].status == TaskStatus.FAILED
    # task_b should remain blocked/pending, never executed
    assert result.tasks["task_b"].status in {TaskStatus.BLOCKED, TaskStatus.PENDING}
    assert result.tasks["task_b"].attempt_count == 0


# ---------------------------------------------------------------------------
# 10. Preserves operation_id
# ---------------------------------------------------------------------------

def test_orchestrator_preserves_operation_id():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()
    seen: list[str] = []

    def op_tool(operation_id: str = None):
        seen.append(operation_id)
        if len(seen) == 1:
            raise RuntimeError("first fail")
        return {"success": True}

    registry.register("op_tool", op_tool)

    execution = Execution(execution_id="exec_orch10", objective="Op ID")
    task = Task(task_id="t_op", execution_id="exec_orch10", title="Op", description="Op test", tool_name="op_tool")
    orig_op = task.operation_id
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run("exec_orch10")

    assert result.tasks["t_op"].operation_id == orig_op
    assert seen[0] == seen[1] == orig_op


# ---------------------------------------------------------------------------
# 11. Does not execute unready tasks
# ---------------------------------------------------------------------------

def test_orchestrator_does_not_execute_unready_tasks():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()
    executed: list[str] = []

    def make_tool(name):
        def tool():
            executed.append(name)
            return {"success": True}
        return tool

    registry.register("tool_a", make_tool("a"))
    registry.register("tool_b", make_tool("b"))

    execution = Execution(execution_id="exec_orch11", objective="Unready")
    task_a = Task(task_id="a", execution_id="exec_orch11", title="A", description="A", tool_name="tool_a")
    task_b = Task(task_id="b", execution_id="exec_orch11", title="B", description="B", tool_name="tool_b", dependencies=["a"])
    execution.add_task(task_a)
    execution.add_task(task_b)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run("exec_orch11")

    assert executed == ["a", "b"]
    assert result.status == ExecutionStatus.COMPLETED


# ---------------------------------------------------------------------------
# 12. Audit trail
# ---------------------------------------------------------------------------

def test_orchestrator_audit_trail():
    store = InMemoryExecutionStore()
    registry = create_default_tool_registry()
    execution = Execution(execution_id="exec_orch12", objective="Audit")
    task = Task(task_id="t_audit", execution_id="exec_orch12", title="Audit", description="Audit trail", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, create_default_strategy_registry())
    orchestrator.run("exec_orch12")

    events = store.get_events("exec_orch12")
    types = [e.event_type for e in events]
    assert EventType.TASK_STARTED in types
    assert EventType.VERIFICATION_STARTED in types
    assert EventType.VERIFICATION_SUCCEEDED in types
    assert EventType.TASK_COMPLETED in types
    assert EventType.EXECUTION_COMPLETED in types
    assert EventType.CHECKPOINT_CREATED in types

    checkpoints = store.get_checkpoints("exec_orch12")
    reasons = [c.reason for c in checkpoints]
    assert "before_tool_execution" in reasons
    assert "after_tool_result" in reasons
    assert "after_verification" in reasons


# ---------------------------------------------------------------------------
# 13. Does not bypass verification (executor VERIFYING → verifier SUCCEEDED)
# ---------------------------------------------------------------------------

def test_orchestrator_does_not_bypass_verification():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def succeeds_but_should_be_rejected():
        return {"success": True, "data": {"count": 0}}

    registry.register("sneaky_tool", succeeds_but_should_be_rejected)

    strategy_registry = VerificationStrategyRegistry()

    def reject_zero(task: Task, execution: Execution) -> VerificationResult:
        if (task.result or {}).get("data", {}).get("count") == 0:
            return VerificationResult(status=VerificationStatus.VERIFIED_FAILURE, message="zero not allowed", evidence={"count": 0})
        return VerificationResult(status=VerificationStatus.VERIFIED_SUCCESS, message="ok", evidence={})

    strategy_registry.register("sneaky_tool", reject_zero)

    execution = Execution(execution_id="exec_orch13", objective="No bypass")
    task = Task(task_id="t_sneaky", execution_id="exec_orch13", title="Sneaky", description="Tool says success but verifier must reject", tool_name="sneaky_tool")
    execution.add_task(task)
    execution.update_task_statuses()
    store.create_execution(execution)

    orchestrator = _make_orchestrator(store, registry, strategy_registry)
    result = orchestrator.run("exec_orch13")

    # If orchestrator bypassed verification, it would be SUCCEEDED after 1 attempt
    # Correct behavior: verifier fails → recovery retries until max_attempts → FAILED
    assert result.tasks["t_sneaky"].status == TaskStatus.FAILED
    assert result.tasks["t_sneaky"].attempt_count == 3
    assert result.status == ExecutionStatus.FAILED

    # Verify audit proves verification was called
    events = store.get_events("exec_orch13")
    assert any(e.event_type == EventType.VERIFICATION_STARTED for e in events)
    assert any(e.event_type == EventType.VERIFICATION_FAILED for e in events)
    # Task must have gone through VERIFYING state
    assert result.tasks["t_sneaky"].verification is not None
    assert result.tasks["t_sneaky"].verification.status == VerificationStatus.VERIFIED_FAILURE
