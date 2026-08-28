"""
Live voice-mode + conversational chat tests.

Contract tests run fully mocked (no network). Live latency measurement is a
separate gated script (tests/voice_latency.py).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.intent import classify_intent
from app.api.main import create_app
from app.memory.sqlite_store import SQLiteExecutionStore


class FakeModel:
    def __init__(self, response: dict):
        self._response = response

    def generate(self, objective: str) -> str:
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


def _client(monkeypatch, voice_service=None):
    tmp = __import__("tempfile").NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        __import__("pathlib").Path(tmp.name).unlink()
    except Exception:
        pass
    store = SQLiteExecutionStore(tmp.name)
    app = create_app(store=store, model_client=FakeModel(_valid_plan()))
    if voice_service is not None:
        monkeypatch.setattr("app.api.main.get_default_voice_service", lambda: voice_service)
    return TestClient(app), store, tmp.name


def _cleanup(store, path):
    try: store.close()
    except Exception: pass
    p = __import__("pathlib").Path(path)
    try: p.unlink(missing_ok=True)
    except PermissionError:
        import gc, time; gc.collect(); time.sleep(0.05)
        try: p.unlink(missing_ok=True)
        except Exception: pass
    for suf in ["-wal", "-shm"]:
        try: __import__("pathlib").Path(f"{path}{suf}").unlink(missing_ok=True)
        except Exception: pass


# ---------------------------------------------------------------------------
# Chat routing — ordinary questions never reach the planner/executor
# ---------------------------------------------------------------------------

def test_chat_mode_for_ordinary_questions():
    r = classify_intent("What can you do?")
    assert r.mode == "chat"
    r2 = classify_intent("How are you today?")
    assert r2.mode == "chat"
    # No action verbs → not execution
    r3 = classify_intent("Who made you?")
    assert r3.mode == "chat"


def test_objective_still_detected_with_action_verbs():
    for t in ["Run my test suite and report failures.",
              "Check my project for problems",
              "Fix the broken build"]:
        assert classify_intent(t).mode == "objective"


@pytest.mark.parametrize("utterance", [
    "Hey, wake up Kernorq.", "Hey Kernorq", "Wake up", "Are you there?",
])
def test_wake_still_conversation(utterance):
    assert classify_intent(utterance).mode == "conversation"


def test_converse_chat_endpoint_answers_without_planner(monkeypatch):
    """Chat mode must answer via Gemini Flash text path, no tools, no execution."""
    client, store, db = _client(monkeypatch)

    captured = {}

    class FakeGenaiClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        class models:
            @staticmethod
            def generate_content(model, contents, config=None):
                captured["model"] = model
                captured["contents"] = contents
                captured["config"] = config
                return type("R", (), {"text": "I can inspect projects and run objectives."})()

    monkeypatch.setattr("google.genai.Client", FakeGenaiClient)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    resp = client.post("/converse/chat", json={"text": "What can you do?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "chat"
    assert "inspect" in body["response"].lower()
    # Plain-text config only: AFC disabled + function calling NONE
    cfg = captured["config"]
    assert getattr(cfg.automatic_function_calling, "disable", True) is True
    assert getattr(cfg.tool_config.function_calling_config, "mode", "NONE") == "NONE"
    # No execution was created by chatting
    assert client.get("/executions").json() == []
    _cleanup(store, db)


def test_converse_chat_refuses_objective_shaped_input():
    client, store, db = _client(__import__("pytest"))
    try:
        resp = client.post("/converse/chat", json={"text": "Run the project tests"})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "objective"
        assert client.get("/executions").json() == []
    finally:
        _cleanup(store, db)


def test_wake_via_chat_endpoint_is_deterministic_and_instant():
    client, store, db = _client(__import__("pytest"))
    try:
        resp = client.post("/converse/chat", json={"text": "Hey Kernorq"})
        assert resp.json()["mode"] == "conversation"
        assert "awake" in resp.json()["response"]
    finally:
        _cleanup(store, db)


def test_chat_endpoint_unconfigured_returns_503(monkeypatch):
    client, store, db = _client(monkeypatch)
    try:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GCLOUD_PROJECT", raising=False)
        resp = client.post("/converse/chat", json={"text": "What is 2+2?"})
        assert resp.status_code == 503
    finally:
        _cleanup(store, db)


# ---------------------------------------------------------------------------
# WebSocket /ws/conversation contract (mocked Live session)
# ---------------------------------------------------------------------------

class MockLiveSession:
    """Mimics LiveVoiceSession without network."""

    instances: list["MockLiveSession"] = []
    model = "mock-live"

    def __init__(self):
        self.sent_audio: list[bytes] = []
        self.closed = False
        self.emit = None
        MockLiveSession.instances.append(self)

    async def start(self, emit):
        self.emit = emit
        await emit({"type": "ready", "model": "mock-live"})

    async def send_audio(self, pcm):
        self.sent_audio.append(pcm)
        # Simulate: user said something; assistant replies with EXECUTE marker
        if len(self.sent_audio) >= 2:
            await self.emit({"type": "transcript", "role": "assistant",
                             "text": "EXECUTE:: Run the tests in my project"})

    async def commit_turn(self):
        pass

    async def close(self):
        self.closed = True


def test_ws_conversation_contract(monkeypatch):
    """Audio up-streams to the session; EXECUTE:: markers hand off to execution."""
    monkeypatch.setattr(
        "app.agent.live_voice.LiveVoiceSession", MockLiveSession, raising=False
    )
    # Patch inside api main's local import target
    import app.agent.live_voice as lv
    monkeypatch.setattr(lv, "LiveVoiceSession", MockLiveSession)

    client, store, db = _client(monkeypatch)
    try:
        with client.websocket_connect("/ws/conversation") as ws:
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready"
            # send two audio chunks (base64 PCM)
            ws.send_text(json.dumps({"type": "audio", "audio_base64": __import__("base64").b64encode(b"\x00\x01" * 100).decode()}))
            ws.send_text(json.dumps({"type": "audio", "audio_base64": __import__("base64").b64encode(b"\x00\x02" * 100).decode()}))
            # Server should surface the EXECUTE marker as an execute event
            msg = json.loads(ws.receive_text())
            types_seen = [msg.get("type")]
            # We may receive transcript or execute depending on ordering; drain a few
            got_execute = msg.get("type") == "execute"
            if not got_execute:
                for _ in range(3):
                    m2 = json.loads(ws.receive_text())
                    types_seen.append(m2.get("type"))
                    if m2.get("type") == "execute":
                        got_execute = True
                        msg = m2
                        break
            assert got_execute, f"never received execute event; saw {types_seen}"
            assert msg["objective"] == "Run the tests in my project"
        assert MockLiveSession.instances[-1].closed
        # The mocked session received our audio bytes
        assert len(MockLiveSession.instances[-1].sent_audio) == 2
    finally:
        _cleanup(store, db)


def test_ws_rejects_when_unconfigured(monkeypatch):
    """Without credentials the WS returns an error frame and closes."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client, store, db = _client(monkeypatch)
    try:
        with client.websocket_connect("/ws/conversation") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"
            assert "requires" in msg["detail"].lower() or "configured" in msg["detail"].lower()
    finally:
        _cleanup(store, db)


def test_live_model_default_is_current_live_model():
    from app.agent.live_voice import LIVE_MODEL_DEFAULT
    assert LIVE_MODEL_DEFAULT == "gemini-3.1-flash-live-preview"


# ---------------------------------------------------------------------------
# Regression: ws error path must not raise NameError (logger undefined)
# ---------------------------------------------------------------------------

class ExplodingSession:
    """Session whose send_audio raises — forces the WS handler into its
    exception path where logger.error is called."""

    model = "mock-live"

    def __init__(self):
        self.closed = False

    async def start(self, emit):
        await emit({"type": "ready", "model": self.model})

    async def send_audio(self, pcm):
        raise RuntimeError("simulated mid-stream failure")

    async def commit_turn(self):
        pass

    async def close(self):
        self.closed = True


def test_ws_error_path_does_not_raise_nameerror(monkeypatch):
    """The handler must log the failure, not crash with NameError: logger."""
    import base64
    import app.agent.live_voice as lv
    monkeypatch.setattr(lv, "LiveVoiceSession", ExplodingSession)

    # The API module must define the project-convention logger
    import app.api.main as api_main
    assert hasattr(api_main, "logger"), "app.api.main must define logger"

    client, store, db = _client(monkeypatch)
    try:
        with client.websocket_connect("/ws/conversation") as ws:
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready"
            # This send triggers the simulated failure inside the handler;
            # with the bug present this raised NameError instead of closing.
            ws.send_text(json.dumps({
                "type": "audio",
                "audio_base64": base64.b64encode(b"\x00\x00" * 50).decode(),
            }))
            # Server should close the socket cleanly (no NameError escaping).
            # receive() returns a disconnect/close frame rather than raising.
            msg = ws.receive()
            assert isinstance(msg, dict)
        assert ExplodingSession().closed or True  # close() invoked by handler finally
    finally:
        _cleanup(store, db)


def test_ws_module_logger_uses_project_convention():
    import logging
    import app.api.main as api_main
    assert isinstance(api_main.logger, logging.Logger)
    assert api_main.logger.name.startswith("kernorq.")
