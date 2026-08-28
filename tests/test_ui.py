"""
UI tests — Phase 3.0 Control Room

Verifies that the frontend treats the backend runtime as sole source of truth
and that the minimal API integration (list + static) works.

Does NOT test orchestration logic duplication — UI must use existing API.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

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


def test_ui_served_at_root():
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        text = resp.text
        # Product identity
        assert "Kernorq" in text
        # Judge demo hero — authoritative workload entry point
        assert "Turn workloads into verified outcomes" in text
        assert "Golden Demo Workload" in text
        assert "Run Workload" in text
        # Composer still available for arbitrary objectives
        assert "Tell Kernorq what you want done..." in text
        assert "Execute Objective" in text
        # Voice affordance present
        assert "mic-btn" in text
        assert 'aria-label="Speak to Kernorq"' in text
        # Suggested objectives actually submit
        assert "Inspect my project and report its structure" in text
        # Primary areas only — internals live in execution details
        for nav in ["Home", "Activity", "Projects", "Settings"]:
            assert f'data-nav="{nav.lower()}"' in text
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_static_files_served():
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        for path in ["/static/index.html", "/static/app.js", "/static/style.css"]:
            resp = client.get(path)
            assert resp.status_code == 200
            assert len(resp.content) > 100
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_list_executions_initially_empty():
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.get("/executions")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_list_executions_after_post():
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        assert resp.status_code == 201
        exec_id = resp.json()["execution_id"]

        resp_list = client.get("/executions")
        assert resp_list.status_code == 200
        lst = resp_list.json()
        assert len(lst) == 1
        assert lst[0]["execution_id"] == exec_id
        assert lst[0]["objective"] == "Inspect workspace"
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_ui_uses_backend_as_source_of_truth():
    """POST creates via orchestrator; GET reflects same tasks/events — no frontend duplication."""
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        fake = FakeModel(_valid_plan())
        app = create_app(store=store, model_client=fake)
        client = TestClient(app)

        # Create
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]

        # Fetch via three separate endpoints — all must be consistent
        r_exec = client.get(f"/executions/{exec_id}").json()
        r_tasks = client.get(f"/executions/{exec_id}/tasks").json()
        r_events = client.get(f"/executions/{exec_id}/events").json()

        assert r_exec["execution_id"] == exec_id
        assert r_exec["status"] == "COMPLETED"
        assert "inspect_workspace" in r_exec["tasks"]
        assert "inspect_workspace" in r_tasks
        assert r_tasks["inspect_workspace"]["status"] == "SUCCEEDED"
        # Events prove backend did the work
        types = [e["event_type"] for e in r_events]
        assert "TASK_STARTED" in types
        assert "VERIFICATION_SUCCEEDED" in types

        # UI's list must also reflect the same execution
        lst = client.get("/executions").json()
        assert any(e["execution_id"] == exec_id and e["status"] == "COMPLETED" for e in lst)
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_ui_task_and_timeline_endpoints():
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]

        # Tasks endpoint
        t = client.get(f"/executions/{exec_id}/tasks")
        assert t.status_code == 200
        assert "inspect_workspace" in t.json()

        # Events timeline
        e = client.get(f"/executions/{exec_id}/events")
        assert e.status_code == 200
        assert len(e.json()) >= 4  # at least TASK_STARTED, VERIFICATION_STARTED, etc.
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)


def test_ui_verification_recovery_visible_via_api():
    """Backend exposes verification/recovery for UI to render, not duplicated in frontend."""
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]

        detail = client.get(f"/executions/{exec_id}").json()
        # Verification results and recovery_history come from backend
        assert "verification_results" in detail
        assert "recovery_history" in detail
        assert "checkpoints" in detail
        # Tasks carry verification
        tasks = client.get(f"/executions/{exec_id}/tasks").json()
        assert tasks["inspect_workspace"]["verification"] is not None
        assert tasks["inspect_workspace"]["verification"]["status"] == "verified_success"
    finally:
        try:
            store.close()
        except Exception:
            pass
        _safe_unlink(db)
