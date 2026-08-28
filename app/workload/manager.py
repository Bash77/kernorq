"""
Workload Manager — Phase 2

Single entry point that composes the full workload pipeline:

    CSV -> WorkloadTask[] -> WorkloadPlan -> execution plan dict
        -> EXISTING create_execution_from_plan / store
        -> EXISTING ExecutionOrchestrator with WorkloadSchedulingPolicy
        -> existing executor / verifier / recovery

No second executor and no duplicate state machine: the manager only builds
the plan, persists it through the existing planner contract, and attaches
the workload scheduling policy to the existing orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.memory.store import ExecutionStore
from app.orchestration.orchestrator import ExecutionOrchestrator
from app.orchestration.planner import create_execution_from_plan
from app.orchestration.state import Execution
from app.orchestration.verifier import VerificationStrategyRegistry
from app.tools.registry import ToolRegistry
from app.workload.models import WorkloadTask
from app.workload.planner import WorkloadPlan, build_workload_plan
from app.workload.scheduling import WorkloadSchedulingPolicy


@dataclass
class WorkloadRunResult:
    execution: Execution
    plan: WorkloadPlan
    planned_order: list[str]


def run_workload(
    tasks: list[WorkloadTask],
    *,
    objective: str,
    store: ExecutionStore,
    tool_registry: ToolRegistry,
    strategy_registry: VerificationStrategyRegistry | None = None,
    external_state_checker: Callable | None = None,
    tool_mapping: dict[str, str] | None = None,
    default_tool_input: dict[str, Any] | None = None,
    tool_inputs: dict[str, dict[str, Any]] | None = None,
    max_attempts: int = 3,
) -> WorkloadRunResult:
    """Plans a workload and executes it through the existing engine,
    honoring workload priority/deadline/dependency ordering at runtime."""
    from app.workload.adapter import to_execution_plan_dict

    if strategy_registry is None:
        from app.orchestration.verifier import create_default_strategy_registry

        strategy_registry = create_default_strategy_registry()

    plan = build_workload_plan(tasks)
    plan_dict = to_execution_plan_dict(
        tasks,
        plan,
        tool_registry,
        objective=objective,
        tool_mapping=tool_mapping,
        default_tool_input=default_tool_input,
        tool_inputs=tool_inputs,
        max_attempts=max_attempts,
    )
    # Evidentiary metadata (ignored by the deterministic plan validator)
    plan_dict["scheduling"] = {
        "policy": "workload_priority",
        "planned_order": list(plan.execution_order),
    }

    execution = create_execution_from_plan(plan_dict, tool_registry)
    store.create_execution(execution)

    orchestrator = ExecutionOrchestrator(
        store=store,
        tool_registry=tool_registry,
        strategy_registry=strategy_registry,
        external_state_checker=external_state_checker,
        scheduling_policy=WorkloadSchedulingPolicy(tasks),
    )
    final_execution = orchestrator.run(execution.execution_id)
    return WorkloadRunResult(
        execution=final_execution,
        plan=plan,
        planned_order=list(plan.execution_order),
    )


__all__ = ["run_workload", "WorkloadRunResult"]
