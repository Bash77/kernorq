"""
Tests for Phase 2.8d — Execution API

Acceptance:
  - POST creates valid execution
  - malformed objective rejected
  - malicious Gemini plan still rejected
  - successful execution reaches COMPLETED
  - failed execution reaches FAILED
  - execution persists in SQLite
  - GET survives process restart
  - nonexistent returns 404
  - task endpoint exposes status/result/verification
  - events endpoint exposes audit trail
  - API never allows clients to directly set status/verification/operation_id
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.memory.sqlite_store import SQLiteExecutionStore
from app.orchestration.state import ExecutionStatus, TaskStatus
from app.tools.registry import ToolRegistry, create_default_tool_registry


class FakeModel:
    def __init__(self, response: dict | str):
        self._response = response

    def generate(self, objective: str) -> str:
        if isinstance(self._response, dict):
            return json.dumps(self._response)
        return str(self._response)


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
                "dependencies": [],
            }
        ],
    }


def _temp_sqlite():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return tmp.name


def _safe_unlink(path: str | Path):
    try:
        Path(path).unlink(missing_ok=True)
    except PermissionError:
        # Windows: file still locked by SQLite, try close and retry (best effort, ignore if still locked)
        try:
            import gc
            import time

            gc.collect()
            time.sleep(0.05)
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    # Also try to remove WAL/SHM
    for suffix in ["-wal", "-shm"]:
        try:
            Path(str(path) + suffix).unlink(missing_ok=True)
        except Exception:
            pass


def test_post_creates_valid_execution():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)

        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["objective"] == "Inspect workspace"
        assert data["status"] == ExecutionStatus.COMPLETED.value
        assert "inspect_workspace" in data["tasks"]
        assert data["tasks"]["inspect_workspace"]["status"] == TaskStatus.SUCCEEDED.value
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_malformed_objective_rejected():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)

        resp = client.post("/executions", json={"objective": ""})
        assert resp.status_code in (400, 422)

        resp = client.post("/executions", json={"objective": "   "})
        assert resp.status_code in (400, 422)

        resp = client.post("/executions", json={})
        assert resp.status_code == 422
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_malicious_gemini_plan_still_rejected():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        # No model_client injection; use llm_output hook to send hostile plan
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)

        hostile = {
            "objective": "Hostile",
            "tasks": [
                {"task_id": "t1", "title": "T1", "description": "D1", "tool_name": "delete_entire_database"},
            ],
        }
        resp = client.post("/executions", json={"objective": "Hostile", "llm_output": hostile})
        assert resp.status_code == 400
        assert "unknown tool" in resp.json()["detail"].lower()
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_successful_execution_reaches_completed():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)

        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        assert resp.json()["status"] == ExecutionStatus.COMPLETED.value
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_failed_execution_reaches_failed():
    db_path = _temp_sqlite()
    try:
        from app.tools.registry import ToolRegistry

        registry = ToolRegistry()

        def bad():
            return {"success": False, "error": {"type": "ValidationError", "message": "bad"}}

        registry.register("bad_tool", bad)

        fake_plan = {
            "objective": "Perm fail",
            "tasks": [{"task_id": "t_perm", "title": "Perm", "description": "Perm", "tool_name": "bad_tool"}],
        }
        fake = FakeModel(fake_plan)
        store = SQLiteExecutionStore(db_path)
        app = create_app(store=store, tool_registry=registry, model_client=fake)
        client = TestClient(app)

        resp = client.post("/executions", json={"objective": "Perm fail"})
        assert resp.status_code == 201
        assert resp.json()["status"] == ExecutionStatus.FAILED.value
        assert resp.json()["tasks"]["t_perm"]["status"] == TaskStatus.FAILED.value
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_execution_persists_in_sqlite():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)

        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]

        # Direct store check
        loaded = store.get_execution(exec_id)
        assert loaded.status == ExecutionStatus.COMPLETED

        # GET via API
        resp2 = client.get(f"/executions/{exec_id}")
        assert resp2.status_code == 200
        assert resp2.json()["execution_id"] == exec_id
        assert resp2.json()["status"] == ExecutionStatus.COMPLETED.value
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_get_survives_process_restart():
    db_path = _temp_sqlite()
    try:
        store1 = SQLiteExecutionStore(db_path)
        fake = FakeModel(_valid_plan())
        app1 = create_app(store=store1, model_client=fake)
        client1 = TestClient(app1)
        resp = client1.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]
        store1.close()

        # Simulate restart: new store + new app instance same file
        store2 = SQLiteExecutionStore(db_path)
        app2 = create_app(store=store2, model_client=fake)
        client2 = TestClient(app2)
        resp2 = client2.get(f"/executions/{exec_id}")
        assert resp2.status_code == 200
        assert resp2.json()["status"] == ExecutionStatus.COMPLETED.value

        # Events also survive
        resp_events = client2.get(f"/executions/{exec_id}/events")
        assert resp_events.status_code == 200
        assert len(resp_events.json()) > 0
        store2.close()
    finally:
        _safe_unlink(db_path)


def test_nonexistent_returns_404():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)

        resp = client.get("/executions/nonexistent_123")
        assert resp.status_code == 404

        resp = client.get("/executions/nonexistent_123/tasks")
        assert resp.status_code == 404

        resp = client.get("/executions/nonexistent_123/events")
        assert resp.status_code == 404
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_task_endpoint_exposes_status_result_verification():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)

        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]

        resp_tasks = client.get(f"/executions/{exec_id}/tasks")
        assert resp_tasks.status_code == 200
        tasks = resp_tasks.json()
        assert "inspect_workspace" in tasks
        t = tasks["inspect_workspace"]
        assert "status" in t
        assert "result" in t
        assert "verification" in t
        assert t["status"] == TaskStatus.SUCCEEDED.value
        assert t["verification"]["status"] == "verified_success"
        assert t["operation_id"] is not None
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_events_endpoint_exposes_audit_trail():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)

        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]

        resp_events = client.get(f"/executions/{exec_id}/events")
        assert resp_events.status_code == 200
        events = resp_events.json()
        types = [e["event_type"] for e in events]
        assert "TASK_STARTED" in types
        assert "VERIFICATION_STARTED" in types
        assert "VERIFICATION_SUCCEEDED" in types
        assert "TASK_COMPLETED" in types
        # Each event has required fields
        for e in events:
            assert "event_id" in e
            assert "execution_id" in e
            assert "timestamp" in e
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)


def test_api_never_allows_direct_status_setting():
    db_path = _temp_sqlite()
    try:
        store = SQLiteExecutionStore(db_path)
        # Hostile tries to set status via llm_output directly to COMPLETED without execution
        hostile = {
            "objective": "Hostile status",
            "status": "COMPLETED",
            "tasks": [
                {
                    "task_id": "t1",
                    "title": "T1",
                    "description": "D1",
                    "tool_name": "inspect_project_workspace",
                    "status": "SUCCEEDED",
                    "verification": {"status": "verified_success"},
                    "operation_id": "hacked",
                }
            ],
        }
        # Even if hostile claims SUCCEEDED, API must execute and assign fresh operation_id
        fake = FakeModel(hostile)
        # We use the llm_output hook to test the actual POST body hostile path
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Hostile status", "llm_output": hostile})
        # Should be rejected or sanitized; in our implementation llm_output hostile with valid tool but sanitized status → still succeeds but not as LLM claimed
        # Check that operation_id not hacked
        if resp.status_code == 201:
            assert resp.json()["tasks"]["t1"]["operation_id"] != "hacked"
            assert resp.json()["status"] == ExecutionStatus.COMPLETED.value  # legitimately completed via execution, not LLM claim
        else:
            # If rejected, also acceptable (trust boundary)
            assert resp.status_code == 400

        # Direct POST with status field at top level should be ignored (Pydantic will ignore extra fields)
        resp2 = client.post("/executions", json={"objective": "Test", "status": "FAILED", "verification": "hacked"})
        # Should still create via real LLM plan, not as FAILED
        assert resp2.status_code == 201
        assert resp2.json()["status"] == ExecutionStatus.COMPLETED.value
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db_path)
