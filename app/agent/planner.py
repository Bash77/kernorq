"""
Gemini Structured Planner Adapter — Phase 2.7b

Trust boundary:
  UNTRUSTED Gemini JSON → deterministic validator → Execution

Gemini may propose only:
  objective, tasks[{task_id,title,description,tool_name,tool_input,dependencies,max_attempts}], execution_id

It may NOT set:
  status, verification, result, error, operation_id, attempt_count, checkpoints, etc.

All proposals are validated via app.orchestration.planner.create_execution_from_plan
which enforces tool existence, dependency correctness, cycle detection, etc.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from app.orchestration.planner import InvalidPlanError, create_execution_from_plan
from app.orchestration.state import Execution
from app.tools.registry import ToolRegistry

ALLOWED_PLAN_FIELDS = {"objective", "tasks", "execution_id"}
ALLOWED_TASK_FIELDS = {"task_id", "title", "description", "tool_name", "tool_input", "dependencies", "max_attempts"}
# Explicitly disallowed — if LLM tries to set these, they are stripped and never propagated
DISALLOWED_TOP_FIELDS = {"status", "verification", "result", "error", "operation_id", "attempt_count", "checkpoints", "verification_results", "last_error", "recovery_history", "current_task_id"}
DISALLOWED_TASK_FIELDS = {"status", "verification", "result", "error", "operation_id", "attempt_count", "created_at", "started_at", "completed_at", "execution_id"}


class ModelClient(Protocol):
    """Protocol for Gemini-compatible model client used in tests."""

    def generate(self, objective: str) -> str:
        """Returns raw JSON string for the objective."""
        ...


def _strip_code_fences(raw: str) -> str:
    """Removes ```json ... ``` fences if present."""
    raw = raw.strip()
    # Match ```json\n...``` or ```\n...```
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    return raw


def _sanitize_task(raw_task: dict[str, Any]) -> dict[str, Any]:
    """Returns task dict containing only allowed fields."""
    sanitized: dict[str, Any] = {}
    for k in ALLOWED_TASK_FIELDS:
        if k in raw_task:
            sanitized[k] = raw_task[k]
    # Defaults for optional
    if "tool_input" not in sanitized:
        sanitized["tool_input"] = {}
    if "dependencies" not in sanitized:
        sanitized["dependencies"] = []
    if "max_attempts" not in sanitized:
        sanitized["max_attempts"] = 3
    # Preserve None tool_name explicitly
    return sanitized


def _sanitize_plan(raw_plan: dict[str, Any]) -> dict[str, Any]:
    """Returns plan dict containing only allowed fields, with sanitized tasks."""
    sanitized: dict[str, Any] = {}
    if "objective" in raw_plan:
        sanitized["objective"] = raw_plan["objective"]
    if "execution_id" in raw_plan:
        sanitized["execution_id"] = raw_plan["execution_id"]
    # tasks sanitization
    raw_tasks = raw_plan.get("tasks", [])
    if not isinstance(raw_tasks, list):
        # Let deterministic planner raise proper error
        sanitized["tasks"] = raw_tasks
        return sanitized
    sanitized["tasks"] = [_sanitize_task(t) if isinstance(t, dict) else t for t in raw_tasks]
    return sanitized


def parse_gemini_plan_output(raw: str | dict[str, Any]) -> dict[str, Any]:
    """
    Parses raw Gemini output into a sanitized plan dict.

    Accepts:
      - dict (already parsed)
      - JSON string (with optional ```json fences)

    Sanitizes to only ALLOWED_PLAN_FIELDS / ALLOWED_TASK_FIELDS.
    Disallowed fields like status/verification/operation_id are stripped.

    Raises:
      InvalidPlanError on malformed JSON or non-dict top level.
    """
    if isinstance(raw, dict):
        raw_dict = raw
    elif isinstance(raw, str):
        stripped = _strip_code_fences(raw)
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise InvalidPlanError(f"Malformed Gemini JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise InvalidPlanError(f"Gemini output must be a JSON object, got {type(parsed).__name__}")
        raw_dict = parsed
    else:
        raise InvalidPlanError(f"Gemini output must be str or dict, got {type(raw).__name__}")

    return _sanitize_plan(raw_dict)


def create_execution_from_gemini_output(
    raw: str | dict[str, Any],
    registry: ToolRegistry,
    objective_override: str | None = None,
    execution_id: str | None = None,
) -> Execution:
    """
    Trust boundary: parses untrusted Gemini output, validates deterministically,
    and returns a validated Execution.

    - Parses JSON (with fence handling)
    - Strips disallowed fields (status, verification, etc.)
    - Optionally overrides objective with user-provided objective
    - Delegates to create_execution_from_plan for full validation

    The resulting Execution status is always PENDING/READY (never COMPLETED/FAILED
    as LLM requested), tasks are PENDING/READY, operation_id is freshly generated,
    and verification is None.
    """
    sanitized = parse_gemini_plan_output(raw)
    if objective_override is not None:
        sanitized["objective"] = objective_override
    if execution_id is not None:
        sanitized["execution_id"] = execution_id

    # Deterministic validation — this is the trust boundary
    return create_execution_from_plan(sanitized, registry)


class GeminiPlanner:
    """
    Adapter that uses a ModelClient to generate a plan for an objective.
    The client is injected for testability (fake vs real Gemini).
    """

    def __init__(self, registry: ToolRegistry, model_client: ModelClient | None = None) -> None:
        self.registry = registry
        self.model_client = model_client

    def plan(self, objective: str, execution_id: str | None = None) -> Execution:
        """
        Generates a validated Execution for the objective.

        If no model_client is provided, raises PlannerError (requires injection).
        """
        if self.model_client is None:
            raise InvalidPlanError("No model_client configured for GeminiPlanner")
        raw_output = self.model_client.generate(objective)
        return create_execution_from_gemini_output(raw_output, self.registry, objective_override=objective, execution_id=execution_id)


__all__ = [
    "ALLOWED_PLAN_FIELDS",
    "ALLOWED_TASK_FIELDS",
    "ModelClient",
    "parse_gemini_plan_output",
    "create_execution_from_gemini_output",
    "GeminiPlanner",
]
