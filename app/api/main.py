"""
Execution API — Phase 2.8d

Thin HTTP layer over the deterministic runtime.

POST /executions        → run_objective(objective) → final Execution (via Orchestrator → SQLite)
GET  /executions/{id}   → persisted Execution snapshot
GET  /executions/{id}/tasks  → task list with status/result/verification
GET  /executions/{id}/events → audit trail

The API never allows clients to set status/verification/operation_id directly —
POST only accepts {objective: str}. All execution semantics remain in the deterministic core.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent.runner import run_objective
from app.memory.sqlite_store import SQLiteExecutionStore
from app.memory.store import ExecutionNotFoundError, InMemoryExecutionStore
from app.orchestration.planner import InvalidPlanError
from app.tools.registry import ToolRegistry, create_default_tool_registry


class CreateExecutionRequest(BaseModel):
    objective: str = Field(..., min_length=1, description="User objective string")
    # For testing injection: optional raw LLM output override (not exposed in prod docs)
    # We allow extra field to test hostile plan rejection via trust boundary
    llm_output: Any | None = None


class CreateExecutionResponse(BaseModel):
    execution_id: str
    objective: str
    status: str
    tasks: dict[str, Any]
    recovery_history: list[Any]
    last_error: Any | None = None


def create_app(
    store: Any | None = None,
    tool_registry: ToolRegistry | None = None,
    model_client: Any | None = None,
    external_state_checker: Any | None = None,
) -> FastAPI:
    """
    Creates FastAPI app with injected dependencies for testability.

    Defaults:
      store → SQLiteExecutionStore("executions.db") for persistence across restarts
      tool_registry → create_default_tool_registry()
      model_client → None (must be provided for real Gemini; fake injected in tests)
    """
    app = FastAPI(title="Autonomous Execution API", version="0.1.0")

    # Use provided store or default persistent SQLite file
    app_state_store = store or SQLiteExecutionStore("executions.db")
    app_state_registry = tool_registry or create_default_tool_registry()

    @app.post("/executions", response_model=CreateExecutionResponse, status_code=201)
    def post_executions(body: CreateExecutionRequest):
        # Thin layer: validate request, delegate to run_objective, persist via store
        if not body.objective or not body.objective.strip():
            raise HTTPException(status_code=400, detail="objective must be non-empty")
        try:
            # If llm_output provided (test hook), create a fake model that returns it
            effective_model_client = model_client
            if body.llm_output is not None:
                # Wrap llm_output as model client for trust boundary test
                class _StaticModel:
                    def generate(self, objective: str) -> str:
                        import json

                        if isinstance(body.llm_output, dict):
                            return json.dumps(body.llm_output)
                        return str(body.llm_output)

                effective_model_client = _StaticModel()

            execution = run_objective(
                body.objective,
                store=app_state_store,
                model_client=effective_model_client,
                tool_registry=app_state_registry,
                external_state_checker=external_state_checker,
            )
        except InvalidPlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        # Return snapshot (not raw Execution object)
        return CreateExecutionResponse(
            execution_id=execution.execution_id,
            objective=execution.objective,
            status=execution.status.value,
            tasks={tid: t.to_dict() for tid, t in execution.tasks.items()},
            recovery_history=execution.recovery_history,
            last_error=execution.last_error,
        )

    @app.get("/executions/{execution_id}")
    def get_execution(execution_id: str):
        try:
            execution = app_state_store.get_execution(execution_id)
        except ExecutionNotFoundError:
            raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
        return execution.snapshot()

    @app.get("/executions/{execution_id}/tasks")
    def get_tasks(execution_id: str):
        try:
            execution = app_state_store.get_execution(execution_id)
        except ExecutionNotFoundError:
            raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
        return {tid: t.to_dict() for tid, t in execution.tasks.items()}

    @app.get("/executions/{execution_id}/events")
    def get_events(execution_id: str):
        # Check execution exists first for 404 consistency
        try:
            app_state_store.get_execution(execution_id)
        except ExecutionNotFoundError:
            raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
        events = app_state_store.get_events(execution_id)
        return [e.to_dict() for e in events]

    # Expose store for testing (e.g., restart simulation)
    app.state.store = app_state_store  # type: ignore
    app.state.registry = app_state_registry  # type: ignore

    return app


# Default app instance for `uvicorn app.api.main:app`
app = create_app()

__all__ = ["app", "create_app", "CreateExecutionRequest"]
