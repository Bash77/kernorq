"""
Integration tests for Phase 2.7c — Agent Wiring (run_objective)

Validates:
  - User objective → GeminiPlanner → validated Execution → Orchestrator → COMPLETED
  - Hostile/malformed Gemini output still rejected via trust boundary
  - Failure → recovery remains deterministic (orchestrator authority)
  - Agent cannot directly mutate execution state (LLM cannot set status/verification)
"""

from __future__ import annotations

import json

import pytest

from app.agent.runner import run_objective
from app.memory.store import InMemoryExecutionStore
from app.orchestration.planner import InvalidPlanError
from app.orchestration.state import EventType, ExecutionStatus, TaskStatus, VerificationStatus
from app.tools.registry import ToolRegistry, create_default_tool_registry


class FakeModel:
    def __init__(self, response: str | dict):
        self._response = response
        self.calls: list[str] = []

    def generate(self, objective: str) -> str:
        self.calls.append(objective)
        if isinstance(self._response, dict):
            return json.dumps(self._response)
        return self._response


def _valid_plan():
    return {
        "objective": "Inspect the project workspace",
        "tasks": [
            {
                "task_id": "inspect_workspace",
                "title": "Inspect workspace",
                "description": "Inspect repo",
                "tool_name": "inspect_project_workspace",
                "tool_input": {"directory_path": "."},
                "dependencies": [],
            }
        ],
    }


def test_run_objective_successful_end_to_end():
    store = InMemoryExecutionStore()
    fake = FakeModel(_valid_plan())
    result = run_objective("Inspect the project workspace", store=store, model_client=fake)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["inspect_workspace"].status == TaskStatus.SUCCEEDED
    assert result.tasks["inspect_workspace"].verification.status == VerificationStatus.VERIFIED_SUCCESS
    assert fake.calls == ["Inspect the project workspace"]

    # Audit proves orchestrator path: executor → verifier
    events = store.get_events(result.execution_id)
    types = [e.event_type for e in events]
    assert EventType.TASK_STARTED in types
    assert EventType.VERIFICATION_STARTED in types
    assert EventType.VERIFICATION_SUCCEEDED in types
    assert EventType.TASK_COMPLETED in types
    assert EventType.EXECUTION_COMPLETED in types


def test_run_objective_hostile_output_still_rejected():
    store = InMemoryExecutionStore()
    hostile = {
        "objective": "Try hostile",
        "status": "COMPLETED",
        "tasks": [
            {
                "task_id": "t1",
                "title": "T1",
                "description": "D1",
                "tool_name": "delete_entire_database",  # unknown tool
                "status": "SUCCEEDED",
                "verification": {"status": "verified_success"},
            }
        ],
    }
    fake = FakeModel(hostile)
    with pytest.raises(InvalidPlanError, match="unknown tool"):
        run_objective("Try hostile", store=store, model_client=fake)

    # Even with unknown tool stripped, hostile status/verification must not propagate
    hostile2 = {
        "objective": "Hostile2",
        "status": "COMPLETED",
        "tasks": [
            {
                "task_id": "t1",
                "title": "T1",
                "description": "D1",
                "tool_name": "inspect_project_workspace",
                "status": "SUCCEEDED",
                "verification": {"status": "verified_success"},
                "operation_id": "hacked",
                "attempt_count": 99,
            }
        ],
    }
    fake2 = FakeModel(hostile2)
    store2 = InMemoryExecutionStore()
    result = run_objective("Hostile2", store=store2, model_client=fake2)
    # Must be legitimately executed, not as LLM claimed
    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["t1"].status == TaskStatus.SUCCEEDED
    assert result.tasks["t1"].operation_id != "hacked"
    assert result.tasks["t1"].attempt_count == 1
    assert result.tasks["t1"].verification.status == VerificationStatus.VERIFIED_SUCCESS


def test_run_objective_malformed_json_rejected():
    store = InMemoryExecutionStore()
    fake = FakeModel("not json {")
    with pytest.raises(InvalidPlanError, match="Malformed Gemini JSON"):
        run_objective("Bad JSON", store=store, model_client=fake)


def test_run_objective_failure_recovery_deterministic():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()
    calls: list[str] = []

    def flaky(operation_id: str = None):
        calls.append(operation_id)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"success": True, "data": {"ok": True}}

    registry.register("flaky_tool", flaky)

    fake_plan = {
        "objective": "Flaky objective",
        "tasks": [
            {"task_id": "t_flaky", "title": "Flaky", "description": "Fails once", "tool_name": "flaky_tool"},
        ],
    }
    fake = FakeModel(fake_plan)
    result = run_objective("Flaky objective", store=store, model_client=fake, tool_registry=registry)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["t_flaky"].status == TaskStatus.SUCCEEDED
    assert result.tasks["t_flaky"].attempt_count == 2
    assert calls[0] == calls[1]  # operation_id preserved
    assert len(result.recovery_history) == 1


def test_run_objective_unknown_recovery_via_external_checker():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def timeout_tool():
        raise TimeoutError("timed out unknown")

    registry.register("timeout_tool", timeout_tool)

    fake_plan = {
        "objective": "Unknown test",
        "tasks": [{"task_id": "t_timeout", "title": "Timeout", "description": "Unknown", "tool_name": "timeout_tool"}],
    }
    fake = FakeModel(fake_plan)

    def checker(task):
        return "FOUND"

    result = run_objective("Unknown test", store=store, model_client=fake, tool_registry=registry, external_state_checker=checker)
    assert result.status == ExecutionStatus.COMPLETED
    assert result.tasks["t_timeout"].status == TaskStatus.SUCCEEDED


def test_run_objective_permanent_failure_stops():
    store = InMemoryExecutionStore()
    registry = ToolRegistry()

    def bad():
        return {"success": False, "error": {"type": "ValidationError", "message": "bad"}}

    registry.register("bad_tool", bad)
    fake_plan = {
        "objective": "Perm fail",
        "tasks": [{"task_id": "t_perm", "title": "Perm", "description": "Perm", "tool_name": "bad_tool"}],
    }
    fake = FakeModel(fake_plan)
    result = run_objective("Perm fail", store=store, model_client=fake, tool_registry=registry)
    assert result.status == ExecutionStatus.FAILED
    assert result.tasks["t_perm"].attempt_count == 1


def test_run_objective_agent_cannot_mutate_state_directly():
    """LLM cannot directly set execution/task status; runner must validate."""
    store = InMemoryExecutionStore()
    # Hostile tries to claim already completed with dependencies bypassed
    hostile = {
        "objective": "Hostile bypass",
        "tasks": [
            {"task_id": "a", "title": "A", "description": "A", "tool_name": "inspect_project_workspace", "status": "SUCCEEDED"},
            {"task_id": "b", "title": "B", "description": "B", "tool_name": "inspect_project_workspace", "dependencies": ["a"], "status": "SUCCEEDED"},
        ],
        "status": "COMPLETED",
    }
    fake = FakeModel(hostile)
    result = run_objective("Hostile bypass", store=store, model_client=fake)

    # Must go through real execution, not LLM's claimed status
    assert result.status == ExecutionStatus.COMPLETED
    # Both tasks must have been executed in order (a before b)
    events = store.get_events(result.execution_id)
    started = [e for e in events if e.event_type == EventType.TASK_STARTED]
    assert [e.task_id for e in started] == ["a", "b"]


def test_run_objective_validates_objective():
    store = InMemoryExecutionStore()
    fake = FakeModel(_valid_plan())
    with pytest.raises(ValueError, match="non-empty string"):
        run_objective("", store=store, model_client=fake)
    with pytest.raises(ValueError, match="non-empty string"):
        run_objective("   ", store=store, model_client=fake)


def test_run_objective_requires_model_client():
    store = InMemoryExecutionStore()
    with pytest.raises(InvalidPlanError, match="No model_client"):
        run_objective("Needs model", store=store, model_client=None)


def test_run_objective_creates_execution_in_store():
    store = InMemoryExecutionStore()
    fake = FakeModel(_valid_plan())
    result = run_objective("Inspect", store=store, model_client=fake)
    # Store must contain execution
    fetched = store.get_execution(result.execution_id)
    assert fetched.objective == "Inspect"
    assert fetched.status == ExecutionStatus.COMPLETED
