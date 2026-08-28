"""
Workload -> Execution Adapter — Phase 1

Thin translation layer between the workload domain and the EXISTING Kernorq
execution system. No second executor: the adapter only produces the plan-dict
contract accepted by app.orchestration.planner.create_execution_from_plan,
which then flows into the existing store -> orchestrator -> verifier ->
recovery pipeline unchanged.

Trust boundary preserved:
- tool names are resolved via an explicit mapping (or default) and validated
  against the real ToolRegistry before any plan is created;
- dependency validation, cycle detection, and status transitions remain the
  responsibility of the existing deterministic planner/orchestrator.
"""

from __future__ import annotations

from typing import Any

from app.workload.models import WorkloadStatus, WorkloadTask
from app.workload.planner import WorkloadPlan

DEFAULT_TOOL_NAME = "inspect_project_workspace"


def to_execution_plan_dict(
    tasks: list[WorkloadTask],
    plan: WorkloadPlan,
    registry: Any,
    *,
    objective: str,
    tool_mapping: dict[str, str] | None = None,
    default_tool_name: str = DEFAULT_TOOL_NAME,
    default_tool_input: dict[str, Any] | None = None,
    tool_inputs: dict[str, dict[str, Any]] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Translates valid workload tasks into the existing execution-plan dict.

    Only READY and BLOCKED tasks are included — the existing orchestrator
    already enforces dependency ordering at runtime. INVALID tasks are
    excluded here (they are reported on the WorkloadPlan) so they can never
    enter execution.

    Args:
        tasks: The full workload task list (source of task data).
        plan: A WorkloadPlan produced by build_workload_plan for these tasks.
        registry: The live ToolRegistry used by the execution system.
        objective: Objective string for the resulting Execution.
        tool_mapping: Optional task_type -> tool_name overrides.
        default_tool_name: Tool for types without a mapping.
        default_tool_input: Input for tools without a per-tool override.
        tool_inputs: Optional tool_name -> input overrides (e.g.,
            ``{"run_test_suite": {"test_path": "tests"}}``) so distinct tools
            can receive their own keyword contract.
        max_attempts: Retry budget per task (existing planner validates 1-5).

    Returns:
        Plan dict accepted by create_execution_from_plan.

    Raises:
        ValueError: if a resolved tool name is not registered.
    """
    mapping = tool_mapping or {}
    per_tool_inputs = tool_inputs or {}

    def resolve_tool(task: WorkloadTask) -> str:
        tool_name = mapping.get(task.task_type, default_tool_name)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(
                f"task '{task.id}': invalid tool name {tool_name!r} for type '{task.task_type}'"
            )
        if not registry.has(tool_name):
            raise ValueError(
                f"task '{task.id}': tool '{tool_name}' is not registered — "
                "workload plans may only use registered tools"
            )
        return tool_name

    executable: list[WorkloadTask] = [
        t for t in tasks if plan.status_of(t.id) in (WorkloadStatus.READY, WorkloadStatus.BLOCKED)
    ]
    ordered = sorted(executable, key=lambda x: plan.execution_order.index(x.id))

    plan_tasks: list[dict[str, Any]] = []
    for t in ordered:
        tool_name = resolve_tool(t)
        tool_input = dict(per_tool_inputs.get(tool_name) or default_tool_input or {})
        plan_tasks.append({
            "task_id": t.id,
            "title": t.title,
            "description": t.description or t.title,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "dependencies": list(t.dependencies),
            "max_attempts": max_attempts,
        })

    return {"objective": objective, "tasks": plan_tasks}


__all__ = ["to_execution_plan_dict", "DEFAULT_TOOL_NAME"]
