"""
Deterministic Plan Model — Phase 2.7a

The LLM proposes; deterministic code validates and converts to Execution.

Boundaries
----------
Planner : validates structured plan → Execution/Task (no tool execution, no LLM)
Executor : executes tools
Verifier : verifies results
Recovery : recovers failures
Orchestrator : runs lifecycle

The planner never invents tools. Unknown tool → plan rejected.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.orchestration.state import Execution, Task
from app.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


class PlannerError(Exception):
    """Base planner validation error."""


class InvalidPlanError(PlannerError):
    """Raised when a plan fails schema/tool/dependency validation."""


@dataclass
class TaskPlan:
    task_id: str
    title: str
    description: str
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _TASK_ID_RE.match(self.task_id):
            raise InvalidPlanError(f"Invalid task_id '{self.task_id}': must match ^[a-zA-Z0-9_\\-]{{1,64}}$")
        if not isinstance(self.title, str) or not self.title.strip():
            raise InvalidPlanError(f"Task '{self.task_id}' requires non-empty title")
        if not isinstance(self.description, str) or not self.description.strip():
            raise InvalidPlanError(f"Task '{self.task_id}' requires non-empty description")
        if self.tool_name is not None and (not isinstance(self.tool_name, str) or not self.tool_name.strip()):
            raise InvalidPlanError(f"Task '{self.task_id}' has invalid tool_name")
        if not isinstance(self.tool_input, dict):
            raise InvalidPlanError(f"Task '{self.task_id}' tool_input must be a dict")
        if not isinstance(self.dependencies, list) or not all(isinstance(d, str) for d in self.dependencies):
            raise InvalidPlanError(f"Task '{self.task_id}' dependencies must be list[str]")
        if self.task_id in self.dependencies:
            raise InvalidPlanError(f"Task '{self.task_id}' cannot depend on itself")
        if not isinstance(self.max_attempts, int) or not (1 <= self.max_attempts <= 5):
            raise InvalidPlanError(f"Task '{self.task_id}' max_attempts must be int in [1,5], got {self.max_attempts}")


@dataclass
class ExecutionPlan:
    objective: str
    tasks: list[TaskPlan] = field(default_factory=list)
    execution_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise InvalidPlanError("ExecutionPlan requires non-empty objective")
        if not isinstance(self.tasks, list) or len(self.tasks) == 0:
            raise InvalidPlanError("ExecutionPlan requires at least one task")
        if self.execution_id is not None and (not isinstance(self.execution_id, str) or not self.execution_id.strip()):
            raise InvalidPlanError("execution_id must be non-empty string if provided")


# ---------------------------------------------------------------------------
# Validation against registry and dependencies
# ---------------------------------------------------------------------------

def _validate_tool_and_dependencies(tasks: list[TaskPlan], registry: ToolRegistry) -> None:
    task_ids = [t.task_id for t in tasks]
    # Duplicate check
    if len(task_ids) != len(set(task_ids)):
        dup = next(t for t in task_ids if task_ids.count(t) > 1)
        raise InvalidPlanError(f"Duplicate task_id '{dup}'")

    ids_set = set(task_ids)

    # Tool validation
    for t in tasks:
        if t.tool_name is not None and not registry.has(t.tool_name):
            raise InvalidPlanError(f"Task '{t.task_id}' references unknown tool '{t.tool_name}'")

    # Missing dependency check
    for t in tasks:
        for dep in t.dependencies:
            if dep not in ids_set:
                raise InvalidPlanError(f"Task '{t.task_id}' depends on missing task '{dep}'")

    # Circular dependency check via DFS
    graph: dict[str, list[str]] = {t.task_id: list(t.dependencies) for t in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> None:
        if node in visiting:
            raise InvalidPlanError(f"Circular dependency detected involving '{node}'")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            dfs(dep)
        visiting.remove(node)
        visited.add(node)

    for tid in task_ids:
        dfs(tid)


def _dict_to_task_plan(raw: dict[str, Any]) -> TaskPlan:
    if not isinstance(raw, dict):
        raise InvalidPlanError(f"Task definition must be a dict, got {type(raw).__name__}")
    # Required fields
    for field_name in ("task_id", "title", "description"):
        if field_name not in raw:
            raise InvalidPlanError(f"Task missing required field '{field_name}'")
    # Build TaskPlan; __post_init__ validates
    return TaskPlan(
        task_id=raw["task_id"],
        title=raw["title"],
        description=raw["description"],
        tool_name=raw.get("tool_name"),
        tool_input=raw.get("tool_input", {}),
        dependencies=raw.get("dependencies", []),
        max_attempts=raw.get("max_attempts", 3),
    )


def _dict_to_execution_plan(raw: dict[str, Any]) -> ExecutionPlan:
    if not isinstance(raw, dict):
        raise InvalidPlanError(f"Execution plan must be a dict, got {type(raw).__name__}")
    if "objective" not in raw:
        raise InvalidPlanError("Execution plan missing required field 'objective'")
    if "tasks" not in raw:
        raise InvalidPlanError("Execution plan missing required field 'tasks'")
    if not isinstance(raw["tasks"], list):
        raise InvalidPlanError("Execution plan 'tasks' must be a list")
    task_plans = [_dict_to_task_plan(t) for t in raw["tasks"]]
    return ExecutionPlan(
        objective=raw["objective"],
        tasks=task_plans,
        execution_id=raw.get("execution_id"),
    )


# ---------------------------------------------------------------------------
# Public API — convert validated plan to Execution
# ---------------------------------------------------------------------------

def create_execution_from_plan(
    plan: ExecutionPlan | dict[str, Any],
    registry: ToolRegistry,
    execution_id: str | None = None,
) -> Execution:
    """
    Validates a structured plan and converts it to an Execution.

    Accepts either an ExecutionPlan dataclass or a raw dict:
      {
        "objective": "...",
        "execution_id": "optional",
        "tasks": [
          {"task_id": "...", "title": "...", "description": "...",
           "tool_name": "...", "tool_input": {...},
           "dependencies": [...], "max_attempts": 3}
        ]
      }

    Validates:
      - non-empty objective and at least one task
      - task_id format, title/description non-empty, tool_input dict, max_attempts 1-5
      - tool_name exists in registry (if not None)
      - no duplicate task_ids
      - no missing dependencies
      - no circular dependencies
      - no self-dependency

    Returns Execution with PENDING/READY tasks (via update_task_statuses).
    Never executes tools.
    """
    if isinstance(plan, dict):
        exec_plan = _dict_to_execution_plan(plan)
    elif isinstance(plan, ExecutionPlan):
        exec_plan = plan
        # Re-validate tool/deps for dataclass path as well
        _validate_tool_and_dependencies(exec_plan.tasks, registry)
    else:
        raise InvalidPlanError(f"Plan must be ExecutionPlan or dict, got {type(plan).__name__}")

    # Validate for dict path (also covers tool/deps)
    if isinstance(plan, dict):
        _validate_tool_and_dependencies(exec_plan.tasks, registry)
    # For ExecutionPlan path, already validated above

    exec_id = execution_id or exec_plan.execution_id or f"exec_{uuid.uuid4().hex[:12]}"

    execution = Execution(execution_id=exec_id, objective=exec_plan.objective)

    for tp in exec_plan.tasks:
        task = Task(
            task_id=tp.task_id,
            execution_id=exec_id,
            title=tp.title,
            description=tp.description,
            tool_name=tp.tool_name,
            tool_input=dict(tp.tool_input),
            dependencies=list(tp.dependencies),
            max_attempts=tp.max_attempts,
        )
        execution.add_task(task)

    # Deterministically set initial READY/BLOCKED based on dependencies
    execution.update_task_statuses()

    return execution


__all__ = ["TaskPlan", "ExecutionPlan", "PlannerError", "InvalidPlanError", "create_execution_from_plan"]
