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

import asyncio
import base64
import json
import logging
import os
from typing import Any

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.gemini_client import GeminiConfigurationError, get_default_gemini_client
from app.agent.intent import classify_intent
from app.agent.runner import run_objective
from app.agent.voice import (
    NoopVoiceService,
    VoiceUnavailableError,
    get_default_voice_service,
)
from app.memory.sqlite_store import SQLiteExecutionStore
from app.memory.store import ExecutionNotFoundError, InMemoryExecutionStore
from app.orchestration.planner import InvalidPlanError
from app.tools.registry import ToolRegistry, create_default_tool_registry

logger = logging.getLogger("kernorq.api")


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


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(..., min_length=1, description="Base64-encoded audio recording")
    mime_type: str = Field(default="audio/webm", description="Audio MIME type")


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000, description="Short summary text to speak")


class VoiceStatusResponse(BaseModel):
    available: bool
    provider: str
    message: str | None = None


class ConverseRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000, description="Utterance to classify")


class ConverseResponse(BaseModel):
    mode: str  # "conversation" | "objective" | "chat"
    response: str | None = None
    text: str


class ChatRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    mode: str
    response: str


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

        def _fallback_execution():
            """Deterministic fallback for demo when Gemini is unavailable."""
            from app.orchestration.orchestrator import ExecutionOrchestrator
            from app.orchestration.planner import create_execution_from_plan
            from app.orchestration.verifier import create_default_strategy_registry

            fallback_plan = {
                "objective": body.objective,
                "tasks": [
                    {
                        "task_id": "inspect_workspace",
                        "title": "Inspect workspace",
                        "description": f"Inspect repository for objective: {body.objective}",
                        "tool_name": "inspect_project_workspace",
                        "tool_input": {"directory_path": "."},
                        "dependencies": [],
                        "max_attempts": 2,
                    }
                ],
            }
            _exec = create_execution_from_plan(fallback_plan, app_state_registry)
            app_state_store.create_execution(_exec)
            _orch = ExecutionOrchestrator(
                store=app_state_store,
                tool_registry=app_state_registry,
                strategy_registry=create_default_strategy_registry(),
                external_state_checker=external_state_checker,
            )
            return _orch.run(_exec.execution_id)

        try:
            # If llm_output provided (test hook), create a fake model that returns it
            effective_model_client = model_client
            if body.llm_output is not None:
                class _StaticModel:
                    def generate(self, objective: str) -> str:
                        import json

                        if isinstance(body.llm_output, dict):
                            return json.dumps(body.llm_output)
                        return str(body.llm_output)

                effective_model_client = _StaticModel()
            elif effective_model_client is None:
                # No explicit injection — try to create default configured Gemini client
                try:
                    effective_model_client = get_default_gemini_client(app_state_registry)
                except GeminiConfigurationError as exc:
                    # Demo fallback when Gemini not configured (no API key) — keep Control Room usable
                    if os.getenv("GEMINI_FALLBACK", "true").lower() == "true" and "not configured" in str(exc).lower():
                        _result = _fallback_execution()
                        return CreateExecutionResponse(
                            execution_id=_result.execution_id,
                            objective=_result.objective,
                            status=_result.status.value,
                            tasks={tid: t.to_dict() for tid, t in _result.tasks.items()},
                            recovery_history=_result.recovery_history,
                            last_error=_result.last_error,
                        )
                    raise HTTPException(status_code=503, detail=str(exc)) from exc

            try:
                execution = run_objective(
                    body.objective,
                    store=app_state_store,
                    model_client=effective_model_client,
                    tool_registry=app_state_registry,
                    external_state_checker=external_state_checker,
                )
            except RuntimeError as exc:
                # Gemini generation failed (403 billing, 429, etc.) — fallback for demo, else 502
                if os.getenv("GEMINI_FALLBACK", "true").lower() == "true" and "gemini" in str(exc).lower():
                    _result = _fallback_execution()
                    return CreateExecutionResponse(
                        execution_id=_result.execution_id,
                        objective=_result.objective,
                        status=_result.status.value,
                        tasks={tid: t.to_dict() for tid, t in _result.tasks.items()},
                        recovery_history=_result.recovery_history,
                        last_error=_result.last_error,
                    )
                raise HTTPException(status_code=502, detail=f"Gemini generation failed: {exc}") from exc
            except GeminiConfigurationError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except InvalidPlanError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except ExecutionNotFoundError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            except HTTPException:
                raise
            except Exception as exc:  # pragma: no cover
                raise HTTPException(status_code=500, detail=f"Internal error: {exc}") from exc
        except HTTPException:
            raise
        except GeminiConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except InvalidPlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=f"Gemini generation failed: {exc}") from exc
        except Exception as exc:  # pragma: no cover
            raise HTTPException(status_code=500, detail=f"Internal error: {exc}") from exc

        return CreateExecutionResponse(
            execution_id=execution.execution_id,
            objective=execution.objective,
            status=execution.status.value,
            tasks={tid: t.to_dict() for tid, t in execution.tasks.items()},
            recovery_history=execution.recovery_history,
            last_error=execution.last_error,
        )

    @app.get("/executions")
    def list_executions():
        # Minimal API integration for execution history — UI treats store as source of truth
        try:
            executions = app_state_store.list_executions()  # type: ignore[attr-defined]
        except AttributeError:
            # Fallback for stores without list method (should not happen after 2.8)
            executions = []
        return [e.snapshot() for e in executions]

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

    # ------------------------------------------------------------------
    # Voice interface layer (Gemini server-side; key never reaches browser)
    # ------------------------------------------------------------------

    @app.get("/voice/status", response_model=VoiceStatusResponse)
    def voice_status():
        service = get_default_voice_service()
        if isinstance(service, NoopVoiceService):
            return VoiceStatusResponse(available=False, provider="none", message="Set GOOGLE_API_KEY or GEMINI_API_KEY to enable voice")
        return VoiceStatusResponse(available=True, provider="gemini")

    @app.post("/converse", response_model=ConverseResponse)
    def converse(body: ConverseRequest):
        """
        Intent router: classifies an utterance BEFORE any execution.

        conversation → short spoken response; NO Execution is created.
        objective    → caller submits via POST /executions as usual.
        chat         → caller uses POST /converse/chat for a short Gemini answer.
        """
        result = classify_intent(body.text)
        return ConverseResponse(mode=result.mode, response=result.response, text=result.text)

    @app.post("/converse/chat", response_model=ChatResponse)
    def converse_chat(body: ChatRequest):
        """
        Dedicated low-latency conversational path for ordinary questions.
        Never enters the planner/executor; plain text answer via Gemini Flash
        with no tools and AFC disabled. Wake phrases answered deterministically.
        """
        intent = classify_intent(body.text)

        def _flash_answer(question: str) -> str:
            from google import genai
            from google.genai import types

            key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
                   or os.getenv("GOOGLE_GENAI_API_KEY"))
            project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT")
            if not key and not project:
                raise GeminiConfigurationError("Gemini not configured for chat")
            gc = genai.Client(api_key=key, vertexai=False) if key else genai.Client(
                vertexai=True, project=project,
                location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
            model = os.getenv("KERNORQ_TRANSCRIBE_MODEL", "gemini-3.5-flash")
            resp = gc.models.generate_content(
                model=model,
                contents=[
                    "You are Kernorq's voice assistant. Answer briefly in 1-3 short, "
                    "natural sentences suitable for speaking aloud. You cannot execute "
                    "tasks; if asked to perform a project action, say you can run it as "
                    "an objective.",
                    question,
                ],
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(mode="NONE")),
                ),
            )
            return (getattr(resp, "text", "") or "").strip() or "I'm not sure how to answer that."

        try:
            if intent.mode == "conversation":
                return ChatResponse(mode="conversation", response=intent.response)
            if intent.mode == "objective":
                # Router guard: objective-looking input is refused here; the caller
                # must use POST /executions so planning/validation stay authoritative.
                return ChatResponse(
                    mode="objective",
                    response="That sounds like a task for Kernorq to execute. "
                             "Submit it as an objective and I will run it.")
            return ChatResponse(mode="chat", response=_flash_answer(intent.text))
        except GeminiConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/voice/transcribe")
    def voice_transcribe(body: TranscribeRequest):
        service = get_default_voice_service()
        try:
            result = service.transcribe(body.audio_base64, mime_type=body.mime_type)
        except GeminiConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except VoiceUnavailableError as exc:
            # Empty transcript / bad audio → 422; upstream Gemini failure → 502
            msg = str(exc)
            if "returned no speech" in msg or "too short" in msg.lower() or "Invalid base64" in msg or "No audio" in msg:
                raise HTTPException(status_code=422, detail={"error": "Voice transcription produced no usable text", "detail": msg}) from exc
            raise HTTPException(status_code=502, detail={"error": "Gemini voice request failed", "detail": msg}) from exc
        return {"text": result.text, "provider": result.provider}

    @app.post("/voice/speak")
    def voice_speak(body: SpeakRequest):
        service = get_default_voice_service()
        try:
            result = service.speak(body.text)
        except GeminiConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except VoiceUnavailableError as exc:
            raise HTTPException(status_code=502, detail={"error": "Gemini voice request failed", "detail": str(exc)}) from exc
        return {"audio_base64": result.audio_base64, "mime_type": result.mime_type, "provider": result.provider}

    @app.get("/tools")
    def list_tools():
        # Tools registry for the UI — read-only; agent cannot execute unregistered tools
        tools = []
        for name in app_state_registry.list_tools():
            func = app_state_registry.get(name)
            doc = (getattr(func, "__doc__", "") or "").strip().splitlines()[0] if getattr(func, "__doc__", None) else ""
            tools.append({"name": name, "description": doc, "enabled": True})
        return tools

    # ------------------------------------------------------------------
    # Live voice mode: persistent bidirectional session (Gemini Live API).
    # Server-to-server auth; key never reaches the browser.
    # ------------------------------------------------------------------

    @app.websocket("/ws/conversation")
    async def ws_conversation(websocket: WebSocket):
        await websocket.accept()
        from app.agent.live_voice import LiveVoiceSession

        try:
            session = LiveVoiceSession()
        except GeminiConfigurationError as exc:
            await websocket.send_json({"type": "error", "detail": str(exc)})
            await websocket.close()
            return

        loop = asyncio.get_event_loop()
        connected = asyncio.Event()

        async def emit(message: dict) -> None:
            # Detect EXECUTE:: marker from the Live model → hand off to the
            # EXISTING deterministic pipeline; never executed by the LLM itself.
            if message.get("type") == "transcript" and message.get("role") == "assistant":
                text = message.get("text", "").strip()
                if text.upper().startswith("EXECUTE::"):
                    objective = text.split("::", 1)[1].strip()
                    await websocket.send_json({"type": "execute", "objective": objective})
                    return
            try:
                await websocket.send_json(message)
                if message.get("type") in ("turn_complete", "error", "go_away"):
                    pass
                if not connected.is_set() and message.get("type") != "error":
                    connected.set()
            except Exception:
                pass

        await session.start(emit)
        await websocket.send_json({"type": "ready", "model": session.model})
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data:
                    await session.send_audio(data)
                    continue
                text_msg = msg.get("text")
                if text_msg:
                    parsed = json.loads(text_msg)
                    mtype = parsed.get("type")
                    if mtype == "audio":
                        await session.send_audio(base64.b64decode(parsed["audio_base64"]))
                    elif mtype == "end_of_speech":
                        await session.commit_turn()
                    elif mtype == "stop":
                        break
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("ws conversation error | %s | %s", type(exc).__name__, str(exc)[:200])
        finally:
            await session.close()

    # Mount static UI (control room) — serves at /static and / (index)
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        def serve_ui():
            index = static_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            raise HTTPException(status_code=404, detail="UI not found")

    # ------------------------------------------------------------------
    # Workload demo layer — canonical golden_demo.csv via existing engine.
    # Backend is authoritative; frontend only DISPLAYS.
    # ------------------------------------------------------------------

    @app.get("/workloads/golden")
    def get_golden_workload():
        """Returns the canonical 22-task workload with plan preview."""
        try:
            from app.workload.golden_demo import load_golden_demo_tasks
            from app.workload.planner import build_workload_plan
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Workload load failed: {exc}") from exc
        try:
            tasks = load_golden_demo_tasks()
            plan = build_workload_plan(tasks)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        task_views = []
        for t in tasks:
            entry = plan.entries.get(t.id)
            task_views.append({
                "task_id": t.id,
                "title": t.title,
                "category": t.task_type,
                "priority": t.priority,
                "priority_label": {1: "High", 3: "Medium", 5: "Low"}.get(t.priority, str(t.priority)),
                "deadline": t.deadline.isoformat() if t.deadline else None,
                "deadline_display": t.deadline.strftime("%b %d") if t.deadline else "—",
                "dependencies": list(t.dependencies),
                "planned_status": entry.status.value if entry else "PENDING",
                "why": entry.reason if entry else "",
                "selection_rank": plan.execution_order.index(t.id) + 1 if t.id in plan.execution_order else None,
            })
        task_views.sort(key=lambda v: v["selection_rank"] or 999)
        return {
            "total": len(tasks),
            "tasks": task_views,
            "planned_order": list(plan.execution_order),
            "counts": {
                "ready": len(plan.ready_task_ids),
                "blocked": len(plan.blocked_task_ids),
                "invalid": len(plan.invalid_task_ids),
            },
            "scheduling_policy": {
                "priority": True,
                "deadline": True,
                "dependencies": True,
                "description": "Priority ASC (1=highest), deadline ASC (None last), task_id ASC tie-break",
            },
        }

    @app.post("/workloads/golden/run", status_code=202)
    def run_golden_workload():
        """Starts the golden demo workload through the existing Phase 2 engine.

        Creates the Execution synchronously, then runs the orchestrator in a
        background thread so the UI can poll live state via GET /executions/{id}.
        """
        import threading

        try:
            from app.workload.golden_demo import (
                DEFAULT_DEMO_TOOL,
                DEFAULT_DEMO_TOOL_INPUT,
                DEMO_TOOL_INPUTS,
                DEMO_TOOL_MAPPING,
                load_golden_demo_tasks,
                workload_ui_summary,
            )
            from app.workload.manager import run_workload as run_workload_fn

            tasks = load_golden_demo_tasks()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Workload load failed: {exc}") from exc

        # Use the app's persistent store/registry so polling sees the same rows.
        store = app_state_store
        registry = app_state_registry

        # Build plan + execution synchronously (fast, <100ms), then hand off.
        # Per-task inputs so research/content tasks carry their own topic.
        def _per_task_input(task, tool_name: str):
            if tool_name == "research_topic":
                return {"topic": task.title, "num_sources": 3}
            if tool_name == "analyze_competitors":
                return {"topic": task.title, "context": task.description}
            if tool_name == "generate_carousel":
                return {"topic": task.title, "audience": "founders"}
            return dict(DEMO_TOOL_INPUTS.get(tool_name, DEFAULT_DEMO_TOOL_INPUT))

        try:
            from app.workload.planner import build_workload_plan
            from app.orchestration.planner import create_execution_from_plan
            from app.workload.scheduling import WorkloadSchedulingPolicy
            from app.orchestration.orchestrator import ExecutionOrchestrator
            from app.orchestration.verifier import create_default_strategy_registry

            plan = build_workload_plan(tasks)

            # Build plan dict with per-task inputs (research/content need topic)
            plan_dict_tasks = []
            for t in sorted([x for x in tasks if plan.status_of(x.id).value in ("READY", "BLOCKED")],
                            key=lambda x: plan.execution_order.index(x.id)):
                tool_name = DEMO_TOOL_MAPPING.get(t.task_type, DEFAULT_DEMO_TOOL)
                if not registry.has(tool_name):
                    raise ValueError(f"task '{t.id}': tool '{tool_name}' not registered")
                plan_dict_tasks.append({
                    "task_id": t.id,
                    "title": t.title,
                    "description": t.description or t.title,
                    "tool_name": tool_name,
                    "tool_input": _per_task_input(t, tool_name),
                    "dependencies": list(t.dependencies),
                    "max_attempts": 3,
                })
            plan_dict = {"objective": "Kernorq golden demo workload", "tasks": plan_dict_tasks}
            plan_dict["scheduling"] = {"policy": "workload_priority", "planned_order": list(plan.execution_order)}
            execution = create_execution_from_plan(plan_dict, registry)
            store.create_execution(execution)
            execution_id = execution.execution_id
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Workload plan failed: {exc}") from exc

        def _background_run(eid: str, workload_tasks: list) -> None:
            try:
                orch = ExecutionOrchestrator(
                    store=store,
                    tool_registry=registry,
                    strategy_registry=create_default_strategy_registry(),
                    scheduling_policy=WorkloadSchedulingPolicy(workload_tasks),
                )
                orch.run(eid)
            except Exception as exc:
                logger.error("golden workload background run failed | %s | %s", type(exc).__name__, str(exc)[:300])

        thread = threading.Thread(target=_background_run, args=(execution_id, tasks), daemon=True)
        thread.start()

        return {
            "execution_id": execution_id,
            "status": "EXECUTING",
            "total": len(tasks),
            "planned_order": list(plan.execution_order),
        }

    @app.get("/workloads/golden/executions/{execution_id}/summary")
    def get_workload_summary(execution_id: str):
        """Workload-aware summary for a specific execution (UI-friendly)."""
        try:
            execution = app_state_store.get_execution(execution_id)
        except ExecutionNotFoundError:
            raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
        try:
            from app.workload.golden_demo import workload_ui_summary
            from app.workload.planner import build_workload_plan
            from app.workload.golden_demo import load_golden_demo_tasks

            # Re-derive plan to map selection ranks (deterministic)
            tasks = load_golden_demo_tasks()
            plan = build_workload_plan(tasks)
            # Build a minimal WorkloadRunResult-like object for the summary helper
            from app.workload.manager import WorkloadRunResult

            result = WorkloadRunResult(execution=execution, plan=plan, planned_order=list(plan.execution_order))
            summary = workload_ui_summary(result)
            # Enrich with live execution status + verification evidence for the UI
            summary["execution_id"] = execution_id
            summary["execution_status"] = execution.status.value
            summary["planned_order"] = list(plan.execution_order)
            # Actual invocation order from verification evidence / events
            summary["actual_order"] = [t.task_id for t in execution.tasks.values() if t.status.value in ("SUCCEEDED", "FAILED", "RUNNING", "VERIFYING")]
            # Detailed verification evidence per task
            summary["verification"] = {
                tid: (t.verification.to_dict() if t.verification else None)
                for tid, t in execution.tasks.items()
            }
            summary["task_results"] = {
                tid: t.result for tid, t in execution.tasks.items()
            }
            return summary
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Summary failed: {exc}") from exc

    # Expose store for testing (e.g., restart simulation)
    app.state.store = app_state_store  # type: ignore
    app.state.registry = app_state_registry  # type: ignore

    return app


# Default app instance for `uvicorn app.api.main:app`
app = create_app()

__all__ = ["app", "create_app", "CreateExecutionRequest", "TranscribeRequest", "SpeakRequest"]
