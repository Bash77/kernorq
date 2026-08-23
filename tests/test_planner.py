"""
Tests for Phase 2.7a — Deterministic Plan Model

Coverage:
  - valid plans become Execution
  - invalid tool names rejected
  - duplicate task IDs rejected
  - missing dependencies rejected
  - circular dependencies rejected
  - invalid max_attempts rejected
  - malformed task definitions rejected
  - dependency ordering works
  - planner never executes tools
  - end-to-end plan -> orchestrator
"""
from __future__ import annotations

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.orchestrator import ExecutionOrchestrator
from app.orchestration.planner import (
    ExecutionPlan,
    InvalidPlanError,
    TaskPlan,
    create_execution_from_plan,
)
from app.orchestration.state import ExecutionStatus, TaskStatus
from app.tools.registry import ToolRegistry, create_default_tool_registry
from app.orchestration.verifier import create_default_strategy_registry


def _valid_plan_dict():
    return {
        "objective": "Get project ready for submission",
        "tasks": [
            {
                "task_id": "inspect",
                "title": "Inspect workspace",
                "description": "Inspect repo",
                "tool_name": "inspect_project_workspace",
                "tool_input": {"directory_path": "."},
                "dependencies": [],
                "max_attempts": 3,
            },
            {
                "task_id": "validate",
                "title": "Validate requirements",
                "description": "Check requirements",
                "tool_name": "inspect_project_workspace",
                "tool_input": {"directory_path": "."},
                "dependencies": ["inspect"],
            },
        ],
    }


def test_planner_valid_plan_becomes_execution():
    registry = create_default_tool_registry()
    plan = _valid_plan_dict()
    execution = create_execution_from_plan(plan, registry)

    assert execution.objective == "Get project ready for submission"
    assert len(execution.tasks) == 2
    assert execution.execution_id.startswith("exec_")
    assert execution.tasks["inspect"].status == TaskStatus.READY
    # validate depends on inspect -> should be PENDING (blocked until parent succeeds)
    assert execution.tasks["validate"].status == TaskStatus.PENDING
    assert execution.tasks["inspect"].tool_name == "inspect_project_workspace"
    assert execution.tasks["validate"].dependencies == ["inspect"]


def test_planner_execution_plan_dataclass_path():
    registry = create_default_tool_registry()
    exec_plan = ExecutionPlan(
        objective="Objective via dataclass",
        tasks=[
            TaskPlan(task_id="t1", title="T1", description="Desc1", tool_name="inspect_project_workspace"),
            TaskPlan(task_id="t2", title="T2", description="Desc2", tool_name="inspect_project_workspace", dependencies=["t1"]),
        ],
    )
    execution = create_execution_from_plan(exec_plan, registry)
    assert len(execution.tasks) == 2
    assert execution.tasks["t1"].status == TaskStatus.READY


def test_planner_invalid_tool_names_rejected():
    registry = create_default_tool_registry()
    plan = _valid_plan_dict()
    plan["tasks"][0]["tool_name"] = "delete_entire_database"
    with pytest.raises(InvalidPlanError, match="unknown tool"):
        create_execution_from_plan(plan, registry)

    # None tool_name is allowed (planner doesn't require tool)
    plan2 = _valid_plan_dict()
    plan2["tasks"][0]["tool_name"] = None
    execution = create_execution_from_plan(plan2, registry)
    assert execution.tasks["inspect"].tool_name is None


def test_planner_duplicate_task_ids_rejected():
    registry = create_default_tool_registry()
    plan = {
        "objective": "Dup test",
        "tasks": [
            {"task_id": "dup", "title": "A", "description": "A", "tool_name": "inspect_project_workspace"},
            {"task_id": "dup", "title": "B", "description": "B", "tool_name": "inspect_project_workspace"},
        ],
    }
    with pytest.raises(InvalidPlanError, match="Duplicate task_id"):
        create_execution_from_plan(plan, registry)


def test_planner_missing_dependencies_rejected():
    registry = create_default_tool_registry()
    plan = {
        "objective": "Missing dep",
        "tasks": [
            {"task_id": "t1", "title": "T1", "description": "D1", "tool_name": "inspect_project_workspace", "dependencies": ["nonexistent"]},
        ],
    }
    with pytest.raises(InvalidPlanError, match="missing task"):
        create_execution_from_plan(plan, registry)


def test_planner_circular_dependencies_rejected():
    registry = create_default_tool_registry()
    plan = {
        "objective": "Circular",
        "tasks": [
            {"task_id": "a", "title": "A", "description": "A", "tool_name": "inspect_project_workspace", "dependencies": ["c"]},
            {"task_id": "b", "title": "B", "description": "B", "tool_name": "inspect_project_workspace", "dependencies": ["a"]},
            {"task_id": "c", "title": "C", "description": "C", "tool_name": "inspect_project_workspace", "dependencies": ["b"]},
        ],
    }
    with pytest.raises(InvalidPlanError, match="Circular dependency"):
        create_execution_from_plan(plan, registry)

    # Self-dependency
    plan2 = {
        "objective": "Self",
        "tasks": [
            {"task_id": "a", "title": "A", "description": "A", "tool_name": "inspect_project_workspace", "dependencies": ["a"]},
        ],
    }
    with pytest.raises(InvalidPlanError, match="cannot depend on itself"):
        create_execution_from_plan(plan2, registry)


def test_planner_invalid_max_attempts_rejected():
    registry = create_default_tool_registry()
    for bad in [0, 6, -1, "3", 3.5, None]:
        plan = {
            "objective": "Bad attempts",
            "tasks": [
                {"task_id": "t1", "title": "T1", "description": "D1", "tool_name": "inspect_project_workspace", "max_attempts": bad},
            ],
        }
        with pytest.raises(InvalidPlanError, match="max_attempts"):
            create_execution_from_plan(plan, registry)

    # Valid boundaries
    for good in [1, 3, 5]:
        plan = {
            "objective": "Good",
            "tasks": [{"task_id": "t1", "title": "T1", "description": "D1", "tool_name": "inspect_project_workspace", "max_attempts": good}],
        }
        exec_ = create_execution_from_plan(plan, registry)
        assert exec_.tasks["t1"].max_attempts == good


def test_planner_malformed_task_definitions_rejected():
    registry = create_default_tool_registry()

    # Missing required field task_id
    plan = {"objective": "Obj", "tasks": [{"title": "T", "description": "D"}]}
    with pytest.raises(InvalidPlanError, match="missing required field"):
        create_execution_from_plan(plan, registry)

    # Missing title
    plan = {"objective": "Obj", "tasks": [{"task_id": "t1", "description": "D"}]}
    with pytest.raises(InvalidPlanError, match="missing required field"):
        create_execution_from_plan(plan, registry)

    # Empty title
    plan = {"objective": "Obj", "tasks": [{"task_id": "t1", "title": "  ", "description": "D"}]}
    with pytest.raises(InvalidPlanError, match="requires non-empty title"):
        create_execution_from_plan(plan, registry)

    # Invalid task_id format
    plan = {"objective": "Obj", "tasks": [{"task_id": "bad id!", "title": "T", "description": "D"}]}
    with pytest.raises(InvalidPlanError, match="Invalid task_id"):
        create_execution_from_plan(plan, registry)

    # tool_input not dict
    plan = {"objective": "Obj", "tasks": [{"task_id": "t1", "title": "T", "description": "D", "tool_input": "not a dict"}]}
    with pytest.raises(InvalidPlanError, match="tool_input must be a dict"):
        create_execution_from_plan(plan, registry)

    # tasks not list
    plan = {"objective": "Obj", "tasks": "not a list"}
    with pytest.raises(InvalidPlanError, match="must be a list"):
        create_execution_from_plan(plan, registry)

    # Empty objective
    plan = {"objective": "   ", "tasks": [{"task_id": "t1", "title": "T", "description": "D"}]}
    with pytest.raises(InvalidPlanError, match="non-empty objective"):
        create_execution_from_plan(plan, registry)

    # No tasks
    plan = {"objective": "Obj", "tasks": []}
    with pytest.raises(InvalidPlanError, match="at least one task"):
        create_execution_from_plan(plan, registry)

    # Task not dict
    plan = {"objective": "Obj", "tasks": ["not a dict"]}
    with pytest.raises(InvalidPlanError, match="must be a dict"):
        create_execution_from_plan(plan, registry)


def test_planner_dependency_ordering_works():
    registry = create_default_tool_registry()
    plan = {
        "objective": "Ordering",
        "tasks": [
            {"task_id": "c", "title": "C", "description": "C", "tool_name": "inspect_project_workspace", "dependencies": ["b"]},
            {"task_id": "a", "title": "A", "description": "A", "tool_name": "inspect_project_workspace", "dependencies": []},
            {"task_id": "b", "title": "B", "description": "B", "tool_name": "inspect_project_workspace", "dependencies": ["a"]},
        ],
    }
    execution = create_execution_from_plan(plan, registry)
    # Regardless of input order, only a should be READY initially
    assert execution.tasks["a"].status == TaskStatus.READY
    assert execution.tasks["b"].status == TaskStatus.PENDING
    assert execution.tasks["c"].status == TaskStatus.PENDING

    # After a succeeds, b becomes READY
    # Simulate via orchestrator
    store = InMemoryExecutionStore()
    store.create_execution(execution)
    orchestrator = ExecutionOrchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run(execution.execution_id)
    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["a"].status == TaskStatus.SUCCEEDED
    assert result.tasks["b"].status == TaskStatus.SUCCEEDED
    assert result.tasks["c"].status == TaskStatus.SUCCEEDED


def test_planner_never_executes_tools():
    registry = ToolRegistry()
    calls: list[str] = []

    def should_not_run():
        calls.append("executed")
        return {"success": True}

    registry.register("should_not_run", should_not_run)
    plan = {
        "objective": "No exec",
        "tasks": [{"task_id": "t1", "title": "T1", "description": "D1", "tool_name": "should_not_run"}],
    }
    execution = create_execution_from_plan(plan, registry)
    assert calls == []
    assert execution.tasks["t1"].status == TaskStatus.READY


def test_planner_end_to_end_via_orchestrator():
    registry = create_default_tool_registry()
    plan = {
        "objective": "E2E",
        "tasks": [
            {"task_id": "t1", "title": "T1", "description": "Inspect", "tool_name": "inspect_project_workspace", "tool_input": {"directory_path": "."}},
            {"task_id": "t2", "title": "T2", "description": "Inspect again", "tool_name": "inspect_project_workspace", "tool_input": {"directory_path": "."}, "dependencies": ["t1"]},
        ],
    }
    execution = create_execution_from_plan(plan, registry)
    store = InMemoryExecutionStore()
    store.create_execution(execution)

    orchestrator = ExecutionOrchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run(execution.execution_id)

    assert result.status == ExecutionStatus.COMPLETED
    assert len(result.tasks) == 2
    assert all(t.status == TaskStatus.SUCCEEDED for t in result.tasks.values())
    assert result.objective == "E2E"


def test_planner_preserves_tool_input_and_dependencies():
    registry = create_default_tool_registry()
    plan = {
        "objective": "Preserve",
        "tasks": [
            {
                "task_id": "t1",
                "title": "T1",
                "description": "D1",
                "tool_name": "inspect_project_workspace",
                "tool_input": {"directory_path": "./custom", "extra": 123},
                "dependencies": [],
                "max_attempts": 2,
            }
        ],
    }
    execution = create_execution_from_plan(plan, registry)
    assert execution.tasks["t1"].tool_input == {"directory_path": "./custom", "extra": 123}
    assert execution.tasks["t1"].max_attempts == 2


def test_planner_custom_execution_id():
    registry = create_default_tool_registry()
    plan = {"objective": "Obj", "execution_id": "exec_custom_123", "tasks": [{"task_id": "t1", "title": "T1", "description": "D1"}]}
    execution = create_execution_from_plan(plan, registry)
    assert execution.execution_id == "exec_custom_123"

    # Also via param
    plan2 = {"objective": "Obj", "tasks": [{"task_id": "t1", "title": "T1", "description": "D1"}]}
    execution2 = create_execution_from_plan(plan2, registry, execution_id="exec_param")
    assert execution2.execution_id == "exec_param"
