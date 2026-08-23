"""
Tests for Phase 2.8a/b/c — SQLite Persistent Runtime

Acceptance:
  - persistence round-trip
  - events survive restart
  - checkpoints survive restart
  - recovery history survives restart
  - attempt count survives restart
  - operation_id survives restart
  - dependencies survive restart
  - VERIFYING execution can resume
  - FAILED execution can resume through recovery
  - completed execution remains completed
  - no duplicate execution caused by reload
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.memory.sqlite_store import SQLiteExecutionStore
from app.memory.store import InMemoryExecutionStore
from app.orchestration.executor import execute_task
from app.orchestration.orchestrator import ExecutionOrchestrator
from app.orchestration.recovery import recover_task
from app.orchestration.state import Execution, ExecutionStatus, Task, TaskStatus, VerificationResult, VerificationStatus, EventType
from app.orchestration.verifier import create_default_strategy_registry, verify_task
from app.tools.registry import ToolRegistry, create_default_tool_registry


def _temp_store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return SQLiteExecutionStore(tmp.name), Path(tmp.name)


def test_persistence_round_trip():
    store, path = _temp_store()
    try:
        execution = Execution(execution_id="exec_persist", objective="Persist test")
        task = Task(task_id="t1", execution_id="exec_persist", title="T1", description="D1", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
        task.operation_id = "op_fixed_123"
        task.attempt_count = 2
        task.result = {"success": True, "data": {"x": 1}}
        task.verification = VerificationResult(status=VerificationStatus.VERIFIED_SUCCESS, message="ok", evidence={"a": 1})
        # Add recovery history
        execution.recovery_history.append({"task_id": "t1", "reason": "TRANSIENT", "recovery_action": "RETRY"})
        execution.last_error = {"type": "RuntimeError", "message": "boom"}
        execution.verification_results.append(task.verification)
        execution.add_task(task)
        execution.update_task_statuses()
        store.create_execution(execution)

        # Simulate restart: new store instance same file
        store2 = SQLiteExecutionStore(str(path))
        loaded = store2.get_execution("exec_persist")

        assert loaded.execution_id == "exec_persist"
        assert loaded.objective == "Persist test"
        assert loaded.tasks["t1"].operation_id == "op_fixed_123"
        assert loaded.tasks["t1"].attempt_count == 2
        assert loaded.tasks["t1"].result == {"success": True, "data": {"x": 1}}
        assert loaded.tasks["t1"].verification.status == VerificationStatus.VERIFIED_SUCCESS
        assert loaded.recovery_history == [{"task_id": "t1", "reason": "TRANSIENT", "recovery_action": "RETRY"}]
        assert loaded.last_error == {"type": "RuntimeError", "message": "boom"}
        assert loaded.verification_results[0].status == VerificationStatus.VERIFIED_SUCCESS
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_events_survive_restart():
    store, path = _temp_store()
    try:
        execution = Execution(execution_id="exec_evt", objective="Events")
        task = Task(task_id="t1", execution_id="exec_evt", title="T1", description="D1", tool_name="inspect_project_workspace")
        execution.add_task(task)
        execution.update_task_statuses()
        store.create_execution(execution)
        registry = create_default_tool_registry()
        # Execute one task to generate events
        execute_task(execution, "t1", store, registry)
        events_before = store.get_events("exec_evt")
        assert len(events_before) > 0

        # Restart
        store2 = SQLiteExecutionStore(str(path))
        events_after = store2.get_events("exec_evt")
        assert len(events_after) == len(events_before)
        assert events_after[0].event_id == events_before[0].event_id
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_checkpoints_survive_restart():
    store, path = _temp_store()
    try:
        execution = Execution(execution_id="exec_chk", objective="Checkpoints")
        task = Task(task_id="t1", execution_id="exec_chk", title="T1", description="D1", tool_name="inspect_project_workspace")
        execution.add_task(task)
        execution.update_task_statuses()
        store.create_execution(execution)
        store.create_checkpoint("exec_chk", reason="before_test", task_id="t1")
        checkpoints_before = store.get_checkpoints("exec_chk")
        assert any(c.reason == "before_test" for c in checkpoints_before)

        store2 = SQLiteExecutionStore(str(path))
        checkpoints_after = store2.get_checkpoints("exec_chk")
        assert len(checkpoints_after) == len(checkpoints_before)
        assert checkpoints_after[0].reason == checkpoints_before[0].reason
        # Also via execution.checkpoints
        loaded = store2.get_execution("exec_chk")
        assert any(c.reason == "before_test" for c in loaded.checkpoints)
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_recovery_history_and_attempt_count_survive_restart():
    store, path = _temp_store()
    try:
        execution = Execution(execution_id="exec_rec", objective="Recovery")
        task = Task(task_id="t1", execution_id="exec_rec", title="T1", description="D1", tool_name="mock", status=TaskStatus.FAILED, attempt_count=2, max_attempts=3)
        task.error = {"type": "RuntimeError", "message": "fail"}
        task.result = {"success": False, "error": task.error}
        task.verification = VerificationResult(status=VerificationStatus.VERIFIED_FAILURE, message="fail", evidence={"error": task.error})
        execution.add_task(task)
        execution.status = ExecutionStatus.VERIFYING
        store.create_execution(execution)
        # Simulate recovery history
        execution = store.get_execution("exec_rec")
        execution.recovery_history.append({"task_id": "t1", "attempt": 1, "recovery_action": "RETRY"})
        execution.tasks["t1"].attempt_count = 2
        store.update_execution(execution)

        store2 = SQLiteExecutionStore(str(path))
        loaded = store2.get_execution("exec_rec")
        assert loaded.recovery_history[0]["recovery_action"] == "RETRY"
        assert loaded.tasks["t1"].attempt_count == 2
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_operation_id_and_dependencies_survive_restart():
    store, path = _temp_store()
    try:
        execution = Execution(execution_id="exec_dep", objective="Deps")
        task_a = Task(task_id="a", execution_id="exec_dep", title="A", description="A", tool_name="inspect_project_workspace")
        task_b = Task(task_id="b", execution_id="exec_dep", title="B", description="B", tool_name="inspect_project_workspace", dependencies=["a"])
        op_a = task_a.operation_id
        op_b = task_b.operation_id
        execution.add_task(task_a)
        execution.add_task(task_b)
        execution.update_task_statuses()
        store.create_execution(execution)

        store2 = SQLiteExecutionStore(str(path))
        loaded = store2.get_execution("exec_dep")
        assert loaded.tasks["a"].operation_id == op_a
        assert loaded.tasks["b"].operation_id == op_b
        assert loaded.tasks["b"].dependencies == ["a"]
        assert loaded.tasks["a"].status == TaskStatus.READY
        assert loaded.tasks["b"].status == TaskStatus.PENDING
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_verifying_execution_can_resume():
    """START → EXECUTING → process dies at VERIFYING → RESTART → resume → COMPLETED"""
    store, path = _temp_store()
    try:
        registry = create_default_tool_registry()
        strategy = create_default_strategy_registry()
        execution = Execution(execution_id="exec_verify_resume", objective="Verify resume")
        task = Task(task_id="t1", execution_id="exec_verify_resume", title="T1", description="D1", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
        execution.add_task(task)
        execution.update_task_statuses()
        store.create_execution(execution)
        # Execute -> VERIFYING
        after_exec = execute_task(execution, "t1", store, registry)
        assert after_exec.tasks["t1"].status == TaskStatus.VERIFYING
        assert after_exec.status == ExecutionStatus.VERIFYING

        # Simulate process death: new store + new orchestrator
        store2 = SQLiteExecutionStore(str(path))
        # Verify execution still VERIFYING
        loaded = store2.get_execution("exec_verify_resume")
        assert loaded.tasks["t1"].status == TaskStatus.VERIFYING
        assert loaded.status == ExecutionStatus.VERIFYING

        # Resume: verify then complete
        after_verify = verify_task(loaded, "t1", store2, strategy)
        assert after_verify.tasks["t1"].status == TaskStatus.SUCCEEDED
        assert after_verify.status == ExecutionStatus.COMPLETED

        # Also test via orchestrator resume
        store3 = SQLiteExecutionStore(str(path))
        # Reset to VERIFYING for orchestrator test
        # Create fresh execution for orchestrator resume path
        execution2 = Execution(execution_id="exec_resume2", objective="Resume via orchestrator")
        task2 = Task(task_id="t1", execution_id="exec_resume2", title="T1", description="D1", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
        execution2.add_task(task2)
        execution2.update_task_statuses()
        store3.create_execution(execution2)
        after_exec2 = execute_task(execution2, "t1", store3, registry)
        assert after_exec2.status == ExecutionStatus.VERIFYING
        # New orchestrator instance after restart
        store4 = SQLiteExecutionStore(str(path))
        orchestrator = ExecutionOrchestrator(store4, registry, strategy)
        result = orchestrator.run("exec_resume2")
        assert result.status == ExecutionStatus.COMPLETED
        store2.close()
        store3.close()
        store4.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_failed_execution_can_resume_through_recovery():
    store, path = _temp_store()
    try:
        registry = ToolRegistry()
        def failing():
            raise RuntimeError("fail once")

        registry.register("failing", failing)
        execution = Execution(execution_id="exec_fail_resume", objective="Fail resume")
        task = Task(task_id="t1", execution_id="exec_fail_resume", title="T1", description="D1", tool_name="failing", max_attempts=3)
        execution.add_task(task)
        execution.update_task_statuses()
        store.create_execution(execution)
        after_exec = execute_task(execution, "t1", store, registry)
        assert after_exec.tasks["t1"].status == TaskStatus.FAILED

        # Simulate restart before recovery
        store2 = SQLiteExecutionStore(str(path))
        loaded = store2.get_execution("exec_fail_resume")
        assert loaded.tasks["t1"].status == TaskStatus.FAILED
        assert loaded.tasks["t1"].attempt_count == 1
        op_before = loaded.tasks["t1"].operation_id

        # Recovery should preserve operation_id and attempt_count
        after_recovery = recover_task(loaded, "t1", store2)
        assert after_recovery.tasks["t1"].status == TaskStatus.READY
        assert after_recovery.tasks["t1"].operation_id == op_before
        assert after_recovery.tasks["t1"].attempt_count == 1  # not incremented until retry execution

        # Now simulate successful retry via new tool that succeeds
        registry2 = ToolRegistry()
        registry2.register("failing", lambda operation_id=None: {"success": True})
        # Need to execute retry: use same store2
        after_retry = execute_task(after_recovery, "t1", store2, registry2)
        assert after_retry.tasks["t1"].status == TaskStatus.VERIFYING
        # Verify success
        strategy = create_default_strategy_registry()
        after_verify = verify_task(after_retry, "t1", store2, strategy)
        assert after_verify.tasks["t1"].status == TaskStatus.SUCCEEDED
        assert after_verify.tasks["t1"].operation_id == op_before
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_completed_execution_remains_completed():
    store, path = _temp_store()
    try:
        registry = create_default_tool_registry()
        strategy = create_default_strategy_registry()
        execution = Execution(execution_id="exec_done", objective="Done")
        task = Task(task_id="t1", execution_id="exec_done", title="T1", description="D1", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
        execution.add_task(task)
        execution.update_task_statuses()
        store.create_execution(execution)
        orchestrator = ExecutionOrchestrator(store, registry, strategy)
        result = orchestrator.run("exec_done")
        assert result.status == ExecutionStatus.COMPLETED

        store2 = SQLiteExecutionStore(str(path))
        loaded = store2.get_execution("exec_done")
        assert loaded.status == ExecutionStatus.COMPLETED
        assert loaded.tasks["t1"].status == TaskStatus.SUCCEEDED
        # Re-running orchestrator should remain completed, not re-execute
        orchestrator2 = ExecutionOrchestrator(store2, registry, strategy)
        result2 = orchestrator2.run("exec_done")
        assert result2.status == ExecutionStatus.COMPLETED
        # No duplicate task execution: attempt_count still 1
        assert result2.tasks["t1"].attempt_count == 1
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_no_duplicate_execution_caused_by_reload():
    store, path = _temp_store()
    try:
        execution = Execution(execution_id="exec_no_dup", objective="No dup")
        task = Task(task_id="t1", execution_id="exec_no_dup", title="T1", description="D1")
        execution.add_task(task)
        store.create_execution(execution)
        with pytest.raises(ValueError, match="already exists"):
            store.create_execution(execution)

        store2 = SQLiteExecutionStore(str(path))
        # Reload should not create duplicate, get_execution works
        loaded = store2.get_execution("exec_no_dup")
        assert loaded.execution_id == "exec_no_dup"
        # Creating with same ID still fails
        with pytest.raises(ValueError, match="already exists"):
            store2.create_execution(loaded)
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)


def test_sqlite_store_interchangeable_with_memory_via_orchestrator():
    """SQLite store must be drop-in replacement for InMemory in orchestrator."""
    for store, path in [(InMemoryExecutionStore(), None), _temp_store()]:
        is_sqlite = path is not None
        try:
            registry = create_default_tool_registry()
            strategy = create_default_strategy_registry()
            exec_id = "exec_interchange" + ("_sqlite" if is_sqlite else "_mem")
            execution = Execution(execution_id=exec_id, objective="Interchange")
            for i in range(2):
                t = Task(task_id=f"t_{i}", execution_id=exec_id, title=f"T{i}", description=f"D{i}", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
                execution.add_task(t)
            execution.update_task_statuses()
            store.create_execution(execution)
            orchestrator = ExecutionOrchestrator(store, registry, strategy)
            result = orchestrator.run(exec_id)
            assert result.status == ExecutionStatus.COMPLETED
            assert all(t.status == TaskStatus.SUCCEEDED for t in result.tasks.values())
        finally:
            if is_sqlite:
                store.close()
                path.unlink(missing_ok=True)


def test_persistence_behaves_equivalent_to_uninterrupted():
    """A persisted execution resumed after interruption must be equivalent to uninterrupted."""
    # Uninterrupted
    mem_store = InMemoryExecutionStore()
    registry = create_default_tool_registry()
    strategy = create_default_strategy_registry()
    exec_id_mem = "exec_uninterrupted"
    execution_mem = Execution(execution_id=exec_id_mem, objective="Uninterrupted")
    for i in range(2):
        t = Task(task_id=f"t_{i}", execution_id=exec_id_mem, title=f"T{i}", description=f"D{i}", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
        execution_mem.add_task(t)
    execution_mem.update_task_statuses()
    mem_store.create_execution(execution_mem)
    orchestrator_mem = ExecutionOrchestrator(mem_store, registry, strategy)
    result_mem = orchestrator_mem.run(exec_id_mem)

    # Interrupted + resumed via SQLite
    store, path = _temp_store()
    try:
        exec_id_sql = "exec_interrupted"
        execution_sql = Execution(execution_id=exec_id_sql, objective="Uninterrupted")
        for i in range(2):
            t = Task(task_id=f"t_{i}", execution_id=exec_id_sql, title=f"T{i}", description=f"D{i}", tool_name="inspect_project_workspace", tool_input={"directory_path": "."})
            execution_sql.add_task(t)
        execution_sql.update_task_statuses()
        store.create_execution(execution_sql)
        # Simulate partial execution: run first task only via orchestrator with max_steps=1? Instead manually execute first task then restart
        # Simpler: execute first task via executor, then restart orchestrator for remainder
        first_task = "t_0"
        after_exec = execute_task(execution_sql, first_task, store, registry)
        after_verify = verify_task(after_exec, first_task, store, strategy)
        assert after_verify.tasks[first_task].status == TaskStatus.SUCCEEDED
        # Simulate process death: new store connection
        store2 = SQLiteExecutionStore(str(path))
        orchestrator2 = ExecutionOrchestrator(store2, registry, strategy)
        result_sql = orchestrator2.run(exec_id_sql)
        assert result_sql.status == ExecutionStatus.COMPLETED
        # Behavioral equivalence: both have 2 succeeded tasks, same objective, same final status
        assert result_sql.status == result_mem.status
        assert len(result_sql.tasks) == len(result_mem.tasks)
        assert all(t.status == TaskStatus.SUCCEEDED for t in result_sql.tasks.values())
        store2.close()
    finally:
        store.close()
        path.unlink(missing_ok=True)
