from __future__ import annotations

import pytest
from app.memory.store import (
    ExecutionNotFoundError,
    InMemoryExecutionStore,
)
from app.orchestration.state import (
    Checkpoint,
    EventType,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    InvalidStateTransitionError,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)


def test_execution_creation():
    execution = Execution(
        execution_id="exec_001",
        objective="Inspect and prepare project",
    )
    assert execution.execution_id == "exec_001"
    assert execution.objective == "Inspect and prepare project"
    assert execution.status == ExecutionStatus.PENDING
    assert len(execution.tasks) == 0
    assert execution.attempt_count == 0
    assert execution.checkpoints == []
    assert execution.verification_results == []
    assert execution.created_at.tzinfo is not None
    assert execution.updated_at.tzinfo is not None


def test_valid_execution_state_transitions():
    execution = Execution(execution_id="exec_002", objective="Test transitions")
    assert execution.status == ExecutionStatus.PENDING

    execution.transition_to(ExecutionStatus.PLANNING)
    assert execution.status == ExecutionStatus.PLANNING

    execution.transition_to(ExecutionStatus.EXECUTING)
    assert execution.status == ExecutionStatus.EXECUTING

    execution.transition_to(ExecutionStatus.VERIFYING)
    assert execution.status == ExecutionStatus.VERIFYING

    execution.transition_to(ExecutionStatus.RECOVERING)
    assert execution.status == ExecutionStatus.RECOVERING

    execution.transition_to(ExecutionStatus.EXECUTING)
    assert execution.status == ExecutionStatus.EXECUTING

    execution.transition_to(ExecutionStatus.VERIFYING)
    assert execution.status == ExecutionStatus.VERIFYING

    execution.transition_to(ExecutionStatus.COMPLETED)
    assert execution.status == ExecutionStatus.COMPLETED


def test_invalid_execution_state_transitions():
    execution = Execution(execution_id="exec_003", objective="Test invalid transitions")
    # PENDING cannot jump straight to COMPLETED
    with pytest.raises(InvalidStateTransitionError, match="Invalid execution transition"):
        execution.transition_to(ExecutionStatus.COMPLETED)

    # Transition to PLANNING
    execution.transition_to(ExecutionStatus.PLANNING)

    # PLANNING cannot jump straight to VERIFYING
    with pytest.raises(InvalidStateTransitionError, match="Invalid execution transition"):
        execution.transition_to(ExecutionStatus.VERIFYING)


def test_execution_cancellation_paths():
    for initial_status in [
        ExecutionStatus.PENDING,
        ExecutionStatus.PLANNING,
        ExecutionStatus.EXECUTING,
        ExecutionStatus.RECOVERING,
    ]:
        execution = Execution(execution_id=f"exec_cancel_{initial_status}", objective="Cancel test")
        execution.status = initial_status
        execution.transition_to(ExecutionStatus.CANCELLED)
        assert execution.status == ExecutionStatus.CANCELLED


def test_task_creation_and_operation_identity():
    task1 = Task(
        task_id="task_1",
        execution_id="exec_004",
        title="Inspect Workspace",
        description="Inspect files in workspace",
        tool_name="inspect_project_workspace",
    )
    assert task1.task_id == "task_1"
    assert task1.status == TaskStatus.PENDING
    assert task1.attempt_count == 0
    assert task1.max_attempts == 3
    assert task1.operation_id is not None
    initial_op_id = task1.operation_id

    # Simulated retry should preserve operation identity for idempotency
    task1.attempt_count += 1
    assert task1.operation_id == initial_op_id


def test_task_state_transitions():
    task = Task(
        task_id="task_transition",
        execution_id="exec_005",
        title="Task Transition Test",
        description="Testing state transitions",
    )
    assert task.status == TaskStatus.PENDING
    assert task.started_at is None
    assert task.completed_at is None

    task.transition_to(TaskStatus.READY)
    assert task.status == TaskStatus.READY

    task.transition_to(TaskStatus.RUNNING)
    assert task.status == TaskStatus.RUNNING
    assert task.started_at is not None

    task.transition_to(TaskStatus.VERIFYING)
    assert task.status == TaskStatus.VERIFYING

    task.transition_to(TaskStatus.SUCCEEDED)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.completed_at is not None

    # Cannot transition from SUCCEEDED to RUNNING
    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(TaskStatus.RUNNING)


def test_task_retry_transition_invariants():
    task = Task(
        task_id="task_retry",
        execution_id="exec_retry",
        title="Task Retry Test",
        description="Testing retry state rules",
    )
    task.transition_to(TaskStatus.READY)
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.FAILED)
    assert task.status == TaskStatus.FAILED

    # FAILED -> RUNNING directly is NOT allowed
    with pytest.raises(InvalidStateTransitionError, match="Invalid task transition"):
        task.transition_to(TaskStatus.RUNNING)

    # Must go through FAILED -> READY -> RUNNING
    task.transition_to(TaskStatus.READY)
    assert task.status == TaskStatus.READY
    task.transition_to(TaskStatus.RUNNING)
    assert task.status == TaskStatus.RUNNING



def test_task_dependency_resolution():
    execution = Execution(execution_id="exec_dep_test", objective="Dependency check")

    task_a = Task(
        task_id="task_a",
        execution_id="exec_dep_test",
        title="Task A",
        description="First task",
    )
    task_b = Task(
        task_id="task_b",
        execution_id="exec_dep_test",
        title="Task B",
        description="Depends on A",
        dependencies=["task_a"],
    )

    execution.add_task(task_a)
    execution.add_task(task_b)

    # Initially task_a is ready, task_b is not
    assert task_a.is_ready(execution.tasks) is True
    assert task_b.is_ready(execution.tasks) is False

    execution.update_task_statuses()
    assert task_a.status == TaskStatus.READY
    assert task_b.status == TaskStatus.PENDING

    # Complete task_a
    task_a.transition_to(TaskStatus.RUNNING)
    task_a.transition_to(TaskStatus.VERIFYING)
    task_a.transition_to(TaskStatus.SUCCEEDED)

    # Now task_b should be ready
    assert task_b.is_ready(execution.tasks) is True
    execution.update_task_statuses()
    assert task_b.status == TaskStatus.READY


def test_blocked_task_on_failed_dependency():
    execution = Execution(execution_id="exec_blocked", objective="Blocked test")

    task_a = Task(
        task_id="task_a",
        execution_id="exec_blocked",
        title="Task A",
        description="First task",
    )
    task_b = Task(
        task_id="task_b",
        execution_id="exec_blocked",
        title="Task B",
        description="Depends on A",
        dependencies=["task_a"],
    )

    execution.add_task(task_a)
    execution.add_task(task_b)

    task_a.transition_to(TaskStatus.READY)
    task_a.transition_to(TaskStatus.RUNNING)
    task_a.transition_to(TaskStatus.FAILED)

    execution.update_task_statuses()
    assert task_b.status == TaskStatus.BLOCKED
    assert task_b.is_ready(execution.tasks) is False


def test_verification_result_semantics():
    v_success = VerificationResult(
        status=VerificationStatus.VERIFIED_SUCCESS,
        message="Tests passed completely",
        evidence={"passed": 5, "failed": 0},
    )
    assert v_success.is_success() is True
    assert v_success.is_failure() is False
    assert v_success.is_unknown() is False

    v_failure = VerificationResult(
        status=VerificationStatus.VERIFIED_FAILURE,
        message="Tests failed",
        evidence={"passed": 4, "failed": 1},
    )
    assert v_failure.is_success() is False
    assert v_failure.is_failure() is True
    assert v_failure.is_unknown() is False

    v_unknown = VerificationResult(
        status=VerificationStatus.UNKNOWN,
        message="Timeout checking test exit status",
    )
    assert v_unknown.is_success() is False
    assert v_unknown.is_failure() is False
    assert v_unknown.is_unknown() is True


def test_checkpoint_immutability():
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_chk", objective="Checkpoint test")
    task = Task(task_id="t1", execution_id="exec_chk", title="T1", description="D1")
    execution.add_task(task)
    store.create_execution(execution)

    checkpoint = store.create_checkpoint(
        execution_id="exec_chk",
        reason="Pre-task execution",
        task_id="t1",
    )

    assert checkpoint.checkpoint_id.startswith("chk_")
    assert checkpoint.state_snapshot["status"] == ExecutionStatus.PENDING.value

    # Mutate execution state afterwards
    execution.transition_to(ExecutionStatus.PLANNING)
    store.update_execution(execution)

    # Checkpoint snapshot should remain unchanged
    assert checkpoint.state_snapshot["status"] == ExecutionStatus.PENDING.value
    stored_checkpoints = store.get_checkpoints("exec_chk")
    assert len(stored_checkpoints) == 1
    assert stored_checkpoints[0].state_snapshot["status"] == ExecutionStatus.PENDING.value


def test_audit_event_append():
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_audit", objective="Audit test")
    store.create_execution(execution)

    event1 = ExecutionEvent(
        event_id="evt_1",
        execution_id="exec_audit",
        event_type=EventType.EXECUTION_CREATED,
        actor="user",
        metadata={"objective": execution.objective},
    )
    event2 = ExecutionEvent(
        event_id="evt_2",
        execution_id="exec_audit",
        event_type=EventType.PLAN_CREATED,
        actor="system",
        metadata={"task_count": 2},
    )

    store.append_event(event1)
    store.append_event(event2)

    events = store.get_events("exec_audit")
    assert len(events) == 2
    assert events[0].event_type == EventType.EXECUTION_CREATED
    assert events[1].event_type == EventType.PLAN_CREATED
    assert events[0].timestamp.tzinfo is not None


def test_execution_store_crud():
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_crud", objective="Store CRUD test")

    # Create
    created = store.create_execution(execution)
    assert created.execution_id == "exec_crud"

    # Duplicate create error
    with pytest.raises(ValueError, match="already exists"):
        store.create_execution(execution)

    # Get
    retrieved = store.get_execution("exec_crud")
    assert retrieved.objective == "Store CRUD test"

    # Get nonexistent error
    with pytest.raises(ExecutionNotFoundError, match="not found"):
        store.get_execution("exec_non_existent")

    # Update
    retrieved.objective = "Updated objective"
    retrieved.transition_to(ExecutionStatus.PLANNING)
    updated = store.update_execution(retrieved)
    assert updated.objective == "Updated objective"
    assert updated.status == ExecutionStatus.PLANNING

    # Update nonexistent error
    fake_execution = Execution(execution_id="exec_fake", objective="Fake")
    with pytest.raises(ExecutionNotFoundError, match="not found"):
        store.update_execution(fake_execution)


def test_execution_store_deep_copy_isolation():
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_iso", objective="Isolation test")
    task = Task(task_id="t1", execution_id="exec_iso", title="T1", description="D1")
    execution.add_task(task)

    # 1. Create execution stores an isolated copy
    store.create_execution(execution)
    # Mutate the local execution object directly without calling update_execution
    execution.objective = "Mutated before update"
    execution.transition_to(ExecutionStatus.PLANNING)
    
    # Store copy must be untouched
    stored = store.get_execution("exec_iso")
    assert stored.objective == "Isolation test"
    assert stored.status == ExecutionStatus.PENDING

    # 2. Retrieved execution is an isolated copy
    retrieved = store.get_execution("exec_iso")
    retrieved.objective = "Mutated after get"
    retrieved.transition_to(ExecutionStatus.PLANNING)

    # Store copy must still be untouched
    stored_again = store.get_execution("exec_iso")
    assert stored_again.objective == "Isolation test"
    assert stored_again.status == ExecutionStatus.PENDING

    # 3. Only explicit update_execution changes the store
    store.update_execution(retrieved)
    stored_updated = store.get_execution("exec_iso")
    assert stored_updated.objective == "Mutated after get"
    assert stored_updated.status == ExecutionStatus.PLANNING

    # 4. Checkpoints and events return isolated copies
    chk = store.create_checkpoint("exec_iso", reason="test_reason")
    chk.reason = "Mutated checkpoint reason"
    retrieved_chks = store.get_checkpoints("exec_iso")
    assert retrieved_chks[0].reason == "test_reason"

    evt = ExecutionEvent(
        event_id="evt_iso",
        execution_id="exec_iso",
        event_type=EventType.EXECUTION_CREATED,
        actor="system",
    )
    store.append_event(evt)
    evt.actor = "mutated_actor"
    retrieved_evts = store.get_events("exec_iso")
    assert retrieved_evts[0].actor == "system"

