"""
Conversational intent routing tests.

Proves:
  - wake/conversational utterances → conversation response, NO Execution
  - execution objectives pass through to the normal pipeline
  - 'analyze_capabilities' MissingToolError cannot recur for wake input
  - TTS is invoked for conversational responses (frontend calls /voice/speak)
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.intent import WAKE_RESPONSE, classify_intent
from app.api.main import create_app
from app.memory.sqlite_store import SQLiteExecutionStore


class FakeModel:
    def __init__(self, response: dict):
        self._response = response

    def generate(self, objective: str) -> str:
        # Simulate the historical defect: LLM invents a tool-less task for chatter.
        if "wake up" in objective.lower():
            return json.dumps({
                "objective": objective,
                "tasks": [{"task_id": "analyze_capabilities", "title": "Analyze", "description": "d"}],
            })
        return json.dumps(self._response)


def _valid_plan():
    return {
        "objective": "Inspect workspace",
        "tasks": [
            {
                "task_id": "inspect_workspace",
                "title": "Inspect workspace",
                "description": "Inspect repo",
                "tool_name": "inspect_project_workspace",
                "tool_input": {"directory_path": "."},
            }
        ],
    }


def _temp_store():
    tmp = __import__("tempfile").NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        __import__("pathlib").Path(tmp.name).unlink()
    except Exception:
        pass
    return tmp.name


def _safe_unlink(p):
    try:
        __import__("pathlib").Path(p).unlink(missing_ok=True)
    except PermissionError:
        import gc, time
        gc.collect(); time.sleep(0.05)
        try: __import__("pathlib").Path(p).unlink(missing_ok=True)
        except Exception: pass
    for suf in ["-wal", "-shm"]:
        try: __import__("pathlib").Path(f"{p}{suf}").unlink(missing_ok=True)
        except Exception: pass


# ---------------------------------------------------------------------------
# Deterministic classifier unit tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("utterance", [
    "Hey, wake up Kernorq.",
    "Hey Kernorq",
    "Wake up",
    "Are you there?",
    "Hello Kernorq",
    "Can you hear me?",
    "Kernorq, are you awake?",
    "kernorq",
    "testing",
])
def test_wake_phrases_classify_as_conversation(utterance):
    result = classify_intent(utterance)
    assert result.mode == "conversation"
    assert result.response == WAKE_RESPONSE


@pytest.mark.parametrize("utterance", [
    "Run my tests.",
    "Inspect my project.",
    "Find problems in my project.",
    "Analyze this repository.",
    "Run the tests and create a report.",
    "Inspect my project and report its structure",
])
def test_execution_objectives_classify_as_objective(utterance):
    result = classify_intent(utterance)
    assert result.mode == "objective"
    assert result.response is None


def test_classifier_is_deterministic():
    for _ in range(3):
        r = classify_intent("Hey, wake up Kernorq.")
        assert (r.mode, r.response) == ("conversation", WAKE_RESPONSE)


# ---------------------------------------------------------------------------
# API-level: /converse + no execution created for conversational input
# ---------------------------------------------------------------------------

def _client():
    db = _temp_store()
    store = SQLiteExecutionStore(db)
    app = create_app(store=store, model_client=FakeModel(_valid_plan()))
    return TestClient(app), store, db


def test_converse_endpoint_conversation():
    client, store, db = _client()
    try:
        r = client.post("/converse", json={"text": "Hey, wake up Kernorq."})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "conversation"
        assert body["response"] == WAKE_RESPONSE
        # Critical invariant: conversational input must NOT create an Execution
        assert client.get("/executions").json() == []
    finally:
        try: store.close()
        except Exception: pass
        _safe_unlink(db)


def test_converse_endpoint_objective_passthrough():
    client, store, db = _client()
    try:
        r = client.post("/converse", json={"text": "Run my tests."})
        assert r.status_code == 200
        assert r.json()["mode"] == "objective"
        assert r.json()["response"] is None
    finally:
        try: store.close()
        except Exception: pass
        _safe_unlink(db)


def test_analyze_capabilities_error_cannot_recur_for_wake_input():
    """
    Regression: previously 'Hey, wake up Kernorq' went to the planner which
    proposed a tool-less task ('analyze_capabilities') -> MissingToolError 500.
    The router must intercept it before any execution is attempted.
    """
    client, store, db = _client()
    try:
        conv = client.post("/converse", json={"text": "Hey, wake up Kernorq."}).json()
        assert conv["mode"] == "conversation"
        # No execution exists — the error path was never entered
        assert client.get("/executions").json() == []
    finally:
        try: store.close()
        except Exception: pass
        _safe_unlink(db)

    # And a hostile/defective tool-less plan sent directly still fails clearly (not silently)
    client2, store2, db2 = _client()
    try:
        hostile = {
            "objective": "chatter",
            "tasks": [{"task_id": "analyze_capabilities", "title": "A", "description": "D"}],
        }
        resp = client2.post("/executions", json={"objective": "chatter", "llm_output": hostile})
        assert resp.status_code in (400, 500)
        if resp.status_code == 500:
            assert "does not have a configured tool" in resp.json()["detail"]
    finally:
        try: store2.close()
        except Exception: pass
        _safe_unlink(db2)


def test_planner_prompt_requires_registered_tool():
    """The planner prompt must demand a registered tool per task (no null tools)."""
    src = __import__("pathlib").Path("app/agent/gemini_client.py").read_text(encoding="utf-8")
    assert "never null" in src or "REQUIRED" in src
    assert "Never omit tool_name" in src


def test_frontend_routes_transcript_through_converse_and_speaks():
    """UI must call /converse after transcription and speak conversational replies."""
    src = __import__("pathlib").Path("app/static/app.js").read_text(encoding="utf-8")
    assert "api.converse(text)" in src
    assert "speakText(intent.response)" in src
    assert "intent.mode==='conversation'" in src
    # Conversational branch must not create an execution
    conv_branch = src.split("intent.mode==='conversation'")[1].split("return;")[0]
    assert "createExecution" not in conv_branch
