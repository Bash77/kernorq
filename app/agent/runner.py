"""
Agent Wiring — Phase 2.7c

Adapter that connects ADK root_agent → deterministic execution system.

Responsibilities:
  - Accept user objective (str)
  - Use GeminiPlanner (untrusted JSON) → deterministic planner validation → Execution
  - Persist Execution via store
  - Run ExecutionOrchestrator (executor → verifier → recovery) to completion
  - Return final verified Execution

Does NOT:
  - Execute tools directly
  - Mutate Task.status / Execution.status directly
  - Call verify/recover bypassing orchestrator
  - Allow LLM to declare completion without verification

The LLM proposes; code executes and verifies.
"""

from __future__ import annotations

from typing import Callable

from app.agent.planner import GeminiPlanner, ModelClient
from app.memory.store import ExecutionStore, InMemoryExecutionStore
from app.orchestration.orchestrator import ExecutionOrchestrator
from app.orchestration.state import Execution
from app.orchestration.verifier import create_default_strategy_registry
from app.tools.registry import ToolRegistry, create_default_tool_registry


def run_objective(
    objective: str,
    *,
    store: ExecutionStore | None = None,
    model_client: ModelClient | None = None,
    tool_registry: ToolRegistry | None = None,
    external_state_checker: Callable | None = None,
    execution_id: str | None = None,
) -> Execution:
    """
    Runs an objective end-to-end through the deterministic pipeline.

    Steps:
      1. Validate objective non-empty
      2. GeminiPlanner.plan(objective) → validated Execution (trust boundary)
      3. store.create_execution(Execution)
      4. ExecutionOrchestrator.run(execution_id) → final Execution (COMPLETED/FAILED)

    Args:
      objective: User goal string (e.g., "Inspect the project workspace")
      store: ExecutionStore (defaults to new InMemoryExecutionStore)
      model_client: ModelClient for Gemini (must be provided for real LLM;
                    fake clients used in tests)
      tool_registry: ToolRegistry (defaults to create_default_tool_registry())
      external_state_checker: optional UNKNOWN handler for recovery
      execution_id: optional fixed execution_id (for testing)

    Returns:
      Final Execution with status COMPLETED or FAILED, fully verified.

    Raises:
      ValueError if objective empty
      InvalidPlanError if Gemini proposes invalid plan (unknown tool, bad deps, etc.)
      Planner errors propagate; execution state never mutated by LLM directly.
    """
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective must be a non-empty string")

    registry = tool_registry or create_default_tool_registry()
    strategy_registry = create_default_strategy_registry()
    exec_store: ExecutionStore = store or InMemoryExecutionStore()

    # 1. LLM proposes → deterministic validation (trust boundary)
    # GeminiPlanner requires model_client; if None, raise as in planner.py
    planner = GeminiPlanner(registry, model_client)
    execution = planner.plan(objective, execution_id=execution_id)

    # 2. Persist validated Execution (no direct status mutation)
    exec_store.create_execution(execution)

    # 3. Orchestrator controls lifecycle (executor/verifier/recovery)
    orchestrator = ExecutionOrchestrator(
        store=exec_store,
        tool_registry=registry,
        strategy_registry=strategy_registry,
        external_state_checker=external_state_checker,
    )
    return orchestrator.run(execution.execution_id)


__all__ = ["run_objective"]
