"""
Workload planning + executor adapter tests — Phase 1.

Covers: all-ready plans, mixed ready/blocked, deterministic plan output,
and translation into the EXISTING execution pipeline (no second executor):
plan dict -> create_execution_from_plan -> orchestrator runs real tools.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.orchestrator import ExecutionOrchestrator
from app.orchestration.planner import create_execution_from_plan
from app.orchestration.state import ExecutionStatus, TaskStatus
from app.tools.registry import ToolRegistry, create_default_tool_registry
from app.workload import (
    WorkloadTask,
    build_workload_plan,
    parse_workload_csv,
    to_execution_plan_dict,
)
from app.workload.models import WorkloadStatus


def _t(tid: str, priority: int = 3, deps: list[str] | None = None) -> WorkloadTask:
    return WorkloadTask(id=tid, title=f"Task {tid}", priority=priority, dependencies=deps or [])


# ---------------------------------------------------------------------------
# Planning classification
# ---------------------------------------------------------------------------

def test_all_tasks_ready_when_no_dependencies():
    tasks = [_t("A"), _t("B"), _t("C")]
    plan = build_workload_plan(tasks)
    assert sorted(plan.ready_task_ids) == ["A", "B", "C"]
    assert plan.blocked_task_ids == []
    assert plan.invalid_task_ids == []
    assert len(plan.execution_order) == 3


def test_mixed_ready_blocked_invalid():
    tasks = [
        _t("FREE1"),
        _t("FREE2"),
        _t("WAITER", deps=["FREE1"]),
        _t("ORPHAN", deps=["NOPE"]),
        _t("BEHIND_ORPHAN", deps=["ORPHAN"]),
    ]
    plan = build_workload_plan(tasks)
    assert set(plan.ready_task_ids) == {"FREE1", "FREE2"}
    assert plan.blocked_task_ids == ["WAITER"]
    assert set(plan.invalid_task_ids) == {"ORPHAN", "BEHIND_ORPHAN"}
    assert set(plan.execution_order) == {"FREE1", "FREE2", "WAITER"}


def test_spec_example_b_never_before_a():
    """Spec: A prio 2; B prio 5 depends on A; C prio 1."""
    csv_text = (
        "id,title,priority,dependencies\n"
        "A,Alpha task,2,\n"
        "B,Beta task,5,A\n"
        "C,Gamma task,1,\n"
    )
    tasks = parse_workload_csv(csv_text)
    plan = build_workload_plan(tasks)
    assert plan.execution_order.index("A") < plan.execution_order.index("B")
    assert plan.status_of("B") == WorkloadStatus.BLOCKED


def test_plan_is_deterministic_regardless_of_input_order():
    tasks = [
        _t("M", 2),
        _t("K", 1),
        _t("Z", 4, deps=["K"]),
        _t("A", 3),
    ]
    p1 = build_workload_plan(tasks)
    p2 = build_workload_plan(list(reversed(tasks)))
    assert p1.execution_order == p2.execution_order
    assert p1.ready_task_ids == p2.ready_task_ids
    assert {k: v.status for k, v in p1.entries.items()} == {k: v.status for k, v in p2.entries.items()}


def test_duplicate_ids_rejected_by_planner():
    with pytest.raises(ValueError, match="duplicate workload task ids"):
        build_workload_plan([_t("DUP"), _t("DUP")])


# ---------------------------------------------------------------------------
# Adapter -> EXISTING executor integration (no second engine)
# ---------------------------------------------------------------------------

def test_adapter_translates_to_existing_plan_contract():
    registry = create_default_tool_registry()
    tasks = [_t("A", 2), _t("B", 5, deps=["A"]), _t("BADREF", deps=["GHOST"])]
    plan = build_workload_plan(tasks)

    plan_dict = to_execution_plan_dict(
        tasks,
        plan,
        registry,
        objective="Deliver the workload",
    )

    from app.workload.models import WorkloadStatus as WS

    # INVALID excluded; READY+BLOCKED included in deterministic order
    ids = [t["task_id"] for t in plan_dict["tasks"]]
    assert ids == ["A", "B"]
    b = next(t for t in plan_dict["tasks"] if t["task_id"] == "B")
    assert b["tool_name"] == "inspect_project_workspace"  # default tool
    assert b["dependencies"] == ["A"]
    assert plan_dict["objective"] == "Deliver the workload"


def test_adapter_rejects_unregistered_tool_mapping():
    """Trust boundary preserved: only registered tools may be mapped."""
    registry = create_default_tool_registry()
    tasks = [_t("A")]
    plan = build_workload_plan(tasks)
    with pytest.raises(ValueError, match="not registered"):
        to_execution_plan_dict(
            tasks,
            plan,
            registry,
            objective="x",
            tool_mapping={"generic": "make_money_fast"},
        )


def test_adapter_type_specific_tool_mapping():
    registry = create_default_tool_registry()
    tasks = [WorkloadTask(id="T1", title="Run tests", task_type="testing")]
    plan = build_workload_plan(tasks)
    plan_dict = to_execution_plan_dict(
        tasks,
        plan,
        registry,
        objective="workload",
        tool_mapping={"testing": "run_test_suite"},
        default_tool_input={"test_path": "tests"},
    )
    assert plan_dict["tasks"][0]["tool_name"] == "run_test_suite"
    assert plan_dict["tasks"][0]["tool_input"] == {"test_path": "tests"}


def test_full_pipeline_csv_to_existing_executor_end_to_end():
    """CSV -> parser -> planner -> adapter -> EXISTING orchestrator completes."""
    csv_text = (
        "id,title,priority,dependencies\n"
        "STEP_A,First step,1,\n"
        "STEP_B,Second step,2,STEP_A\n"
        "STEP_C,Third step,3,STEP_B\n"
    )
    tasks = parse_workload_csv(csv_text)
    plan = build_workload_plan(tasks)
    registry = create_default_tool_registry()

    plan_dict = to_execution_plan_dict(tasks, plan, registry, objective="Execute workload CSV")

    store = InMemoryExecutionStore()
    execution = create_execution_from_plan(plan_dict, registry)
    store.create_execution(execution)
    orchestrator = ExecutionOrchestrator(store, registry, __import__(
        "app.orchestration.verifier", fromlist=["create_default_strategy_registry"]
    ).create_default_strategy_registry())
    result = orchestrator.run(execution.execution_id)

    assert result.status == ExecutionStatus.COMPLETED
    assert all(t.status == TaskStatus.SUCCEEDED for t in result.tasks.values())
    # Runtime honored dependency ordering: A verified before B started
    events = store.get_events(result.execution_id)
    a_completed_at = next(i for i, e in enumerate(events) if e.task_id == "STEP_A" and e.event_type.value == "TASK_COMPLETED")
    b_started_at = next(i for i, e in enumerate(events) if e.task_id == "STEP_B" and e.event_type.value == "TASK_STARTED")
    assert a_completed_at < b_started_at


def test_adapter_never_executes_anything():
    """Planning + adaptation must not invoke any tool."""
    registry = ToolRegistry()
    calls: list[str] = []

    def spy_tool():
        calls.append("called")
        return {"success": True}

    registry.register("spy_tool", spy_tool)
    tasks = [_t("A")]
    plan = build_workload_plan(tasks)
    plan_dict = to_execution_plan_dict(
        tasks, plan, registry, objective="x",
        tool_mapping={"generic": "spy_tool"},
    )
    assert calls == []
    assert plan_dict["tasks"][0]["tool_name"] == "spy_tool"
