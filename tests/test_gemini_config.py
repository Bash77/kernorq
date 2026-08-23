"""
Tests for Phase 3.0 integration fix — Gemini model client configuration

Proves:
  - POST works when model_client is configured/injected (existing injection still works)
  - Missing Gemini configuration produces clear 503 error (not crash)
  - Hostile output still sanitized via trust boundary
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


def _temp_store():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return tmp.name


def _safe_unlink(p):
    try:
        Path(p).unlink(missing_ok=True)
    except PermissionError:
        import gc, time

        gc.collect()
        time.sleep(0.05)
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    for suf in ["-wal", "-shm"]:
        try:
            Path(f"{p}{suf}").unlink(missing_ok=True)
        except Exception:
            pass


def test_post_with_injected_model_client_still_works():
    """Existing injection path must continue working after wiring fix."""
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        assert resp.status_code == 201
        assert resp.json()["status"] == "COMPLETED"
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_missing_gemini_config_returns_503(monkeypatch):
    """When no model_client injected and no credentials, API returns clear 503, not 500 crash."""
    # Ensure no credentials in env and fallback disabled for strict test
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_FALLBACK", "false")

    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        # No model_client injected, no llm_output
        app = create_app(store=store, model_client=None)
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        assert resp.status_code == 503
        detail = resp.json()["detail"].lower()
        assert "gemini" in detail or "api" in detail or "credential" in detail or "not configured" in detail
        # Ensure not a crash (500)
        assert resp.status_code != 500
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_missing_config_with_fallback_returns_201(monkeypatch):
    """With GEMINI_FALLBACK=true (default demo mode), missing credentials fallback to deterministic plan and returns 201."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_FALLBACK", "true")

    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=None)
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        assert resp.status_code == 201
        assert resp.json()["status"] == "COMPLETED"
        assert "inspect_workspace" in resp.json()["tasks"]
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_injected_client_overrides_missing_env(monkeypatch):
    """Injected fake should work even when env missing (injection takes precedence)."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        assert resp.status_code == 201
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_hostile_still_sanitized_via_default_wiring(monkeypatch):
    """Hostile plan via llm_output hook still sanitized even with new wiring."""
    # Ensure default client would fail if used, but llm_output bypasses it
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        # App with no default client (will 503 normally), but llm_output bypasses default
        app = create_app(store=store, model_client=None)
        client = TestClient(app)

        hostile = {
            "objective": "Hostile",
            "tasks": [{"task_id": "t1", "title": "T1", "description": "D1", "tool_name": "delete_entire_database"}],
        }
        resp = client.post("/executions", json={"objective": "Hostile", "llm_output": hostile})
        assert resp.status_code == 400
        assert "unknown tool" in resp.json()["detail"].lower()

        # Hostile status/verification stripped but still succeeds via real execution
        valid_hostile_status = {
            "objective": "Inspect workspace",
            "status": "COMPLETED",
            "tasks": [
                {
                    "task_id": "inspect_workspace",
                    "title": "Inspect workspace",
                    "description": "Inspect repo",
                    "tool_name": "inspect_project_workspace",
                    "tool_input": {"directory_path": "."},
                    "status": "SUCCEEDED",
                    "operation_id": "hacked",
                }
            ],
        }
        resp2 = client.post("/executions", json={"objective": "Inspect workspace", "llm_output": valid_hostile_status})
        assert resp2.status_code == 201
        assert resp2.json()["tasks"]["inspect_workspace"]["operation_id"] != "hacked"
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_configured_client_via_env(monkeypatch):
    """When env has API key, default client should be created without needing real network."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-test-key")
    monkeypatch.setenv("MODEL_NAME", "gemini-3.5-flash")

    from app.agent.gemini_client import get_default_gemini_client
    from app.tools.registry import create_default_tool_registry
    from unittest.mock import MagicMock, patch

    registry = create_default_tool_registry()
    # Mock the genai.Client to avoid real network
    with patch("google.genai.Client") as MockClient:
        mock_instance = MagicMock()
        mock_instance.models.generate_content.return_value = type("Resp", (), {"text": json.dumps(_valid_plan())})()
        MockClient.return_value = mock_instance

        client = get_default_gemini_client(registry)
        assert client is not None
        # Generate should work and return JSON via mocked client
        out = client.generate("Inspect workspace")
        parsed = json.loads(out)
        assert "tasks" in parsed
        MockClient.assert_called_once()
