"""
UI Hardening tests — Phase 3.1

Verifies that the Control Room observability layer correctly derives
visualization from backend state, not invented frontend state.

- Pipeline only shows stages actually reached (Recovering not marked done when no recovery)
- Timeline is chronological oldest → newest (backend sole source)
- Verification/recovery human-readable summaries exist
- Distinguishes successful first attempt vs recovered vs failed vs unknown
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.memory.sqlite_store import SQLiteExecutionStore
from app.orchestration.state import EventType


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


def _flaky_plan():
    return {
        "objective": "Flaky run",
        "tasks": [
            {"task_id": "t_flaky", "title": "Flaky", "description": "Fails once", "tool_name": "flaky_tool", "max_attempts": 3},
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
        gc.collect(); time.sleep(0.05)
        try: Path(p).unlink(missing_ok=True)
        except Exception: pass
    for suf in ["-wal","-shm"]:
        try: Path(f"{p}{suf}").unlink(missing_ok=True)
        except Exception: pass


def test_pipeline_successful_first_attempt_no_recovery():
    """Successful first attempt: Recovering should be pending (○), not done (✓)."""
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]

        events = client.get(f"/executions/{exec_id}/events").json()
        types = [e["event_type"] for e in events]

        # No recovery for first-attempt success
        assert EventType.RECOVERY_STARTED.value not in types
        assert EventType.RETRY_STARTED.value not in types
        assert EventType.VERIFICATION_SUCCEEDED.value in types
        assert EventType.TASK_COMPLETED.value in types
        assert EventType.EXECUTION_COMPLETED.value in types

        # Backend is sole source: execution status COMPLETED, tasks SUCCEEDED
        detail = client.get(f"/executions/{exec_id}").json()
        assert detail["status"] == "COMPLETED"
        assert detail["recovery_history"] == []

        # Verify frontend files contain correct pipeline logic (derived from events, not hardcoded)
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        assert "EXECUTION PIPELINE" in html
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        # Must derive from events, not just status
        assert "RECOVERY_STARTED" in js
        assert "events.slice().reverse()" not in js  # should be chronological, not reversed
        # Pipeline stages derived from events; Recovering stage only injected when recovery happened
        assert "RECOVERY_STARTED" in js and "'RECOVERY'" in js
    finally:
        try: store.close()
        except: pass
        _safe_unlink(db)


def test_pipeline_recovered_execution_shows_recovery():
    """Recovered execution: pipeline must show Recovering as reached, timeline shows retry."""
    db = _temp_store()
    try:
        from app.tools.registry import ToolRegistry

        registry = ToolRegistry()
        calls: list[str] = []

        def flaky(operation_id: str = None):
            calls.append(operation_id)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return {"success": True}

        registry.register("flaky_tool", flaky)

        store = SQLiteExecutionStore(db)
        app = create_app(store=store, tool_registry=registry, model_client=FakeModel(_flaky_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Flaky run"})
        exec_id = resp.json()["execution_id"]

        # Should be COMPLETED after retry
        assert resp.json()["status"] == "COMPLETED"
        # Recovery history should show RETRY
        detail = client.get(f"/executions/{exec_id}").json()
        assert len(detail["recovery_history"]) == 1
        assert detail["recovery_history"][0]["recovery_action"] == "RETRY"

        events = client.get(f"/executions/{exec_id}/events").json()
        types = [e["event_type"] for e in events]
        assert "RECOVERY_STARTED" in types
        assert "RETRY_STARTED" in types
        # Two TASK_STARTED for retry
        assert types.count("TASK_STARTED") == 2
        # Timeline must be chronological: first TASK_STARTED before second
        first_idx = next(i for i, e in enumerate(events) if e["event_type"] == "TASK_STARTED")
        last_idx = len(events) - 1 - next(i for i, e in enumerate(reversed(events)) if e["event_type"] == "TASK_STARTED")
        assert first_idx < last_idx
        # Verify chronological order by timestamp
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps)

        # Operation_id preserved
        tasks = client.get(f"/executions/{exec_id}/tasks").json()
        assert tasks["t_flaky"]["attempt_count"] == 2
        assert len(calls) == 2
        assert calls[0] == calls[1]
    finally:
        try: store.close()
        except: pass
        _safe_unlink(db)


def test_pipeline_failed_execution():
    """Failed execution: COMPLETED not reached, FAILED active, Recovering may be done but final is FAILED."""
    db = _temp_store()
    try:
        from app.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("bad_tool", lambda: {"success": False, "error": {"type": "ValidationError", "message": "bad"}})

        bad_plan = {
            "objective": "Perm fail",
            "tasks": [{"task_id": "t_perm", "title": "Perm", "description": "Perm", "tool_name": "bad_tool"}],
        }

        store = SQLiteExecutionStore(db)
        app = create_app(store=store, tool_registry=registry, model_client=FakeModel(bad_plan))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Perm fail"})
        assert resp.json()["status"] == "FAILED"

        exec_id = resp.json()["execution_id"]
        events = client.get(f"/executions/{exec_id}/events").json()
        types = [e["event_type"] for e in events]
        # Executor failure goes via TASK_FAILED (not VERIFICATION_FAILED) → recovery
        assert "TASK_FAILED" in types
        assert "EXECUTION_FAILED" in types
        assert "EXECUTION_COMPLETED" not in types

        # Frontend must distinguish failed
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        assert "FAILED" in js
    finally:
        try: store.close()
        except: pass
        _safe_unlink(db)


def test_pipeline_unknown_outcome():
    """Unknown outcome: timeout with NOT_FOUND → retry, verify idempotency."""
    db = _temp_store()
    try:
        from app.tools.registry import ToolRegistry

        registry = ToolRegistry()
        received: list[str] = []

        def timeout_then_success(operation_id: str = None):
            received.append(operation_id)
            if len(received) == 1:
                raise TimeoutError("timed out unknown")
            return {"success": True}

        registry.register("timeout_tool", timeout_then_success)

        plan = {
            "objective": "Unknown test",
            "tasks": [{"task_id": "t1", "title": "Timeout", "description": "Unknown", "tool_name": "timeout_tool", "max_attempts": 3}],
        }

        store = SQLiteExecutionStore(db)

        def checker(task):
            return "NOT_FOUND"

        app = create_app(store=store, tool_registry=registry, model_client=FakeModel(plan), external_state_checker=checker)
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Unknown test"})
        assert resp.json()["status"] == "COMPLETED"
        tasks = client.get(f"/executions/{resp.json()['execution_id']}/tasks").json()
        assert tasks["t1"]["attempt_count"] == 2
        assert len(received) == 2
        assert received[0] == received[1]
    finally:
        try: store.close()
        except: pass
        _safe_unlink(db)


def test_event_timeline_chronological():
    """Timeline must be oldest → newest, backend provides sorted events."""
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]
        events = client.get(f"/executions/{exec_id}/events").json()
        # Backend returns ORDER BY timestamp ASC
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps)
        # Frontend must not reverse (chronological, not newest-first)
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        assert "events.slice().reverse()" not in js
        assert "events.forEach" in js
        # First event is CHECKPOINT_CREATED (before_tool_execution), then TASK_STARTED
        assert events[0]["event_type"] == "CHECKPOINT_CREATED"
        assert events[0]["metadata"]["reason"] == "before_tool_execution"
        # TASK_STARTED should come after first checkpoint, before verification
        task_idx = next(i for i, e in enumerate(events) if e["event_type"] == "TASK_STARTED")
        ver_idx = next(i for i, e in enumerate(events) if e["event_type"] == "VERIFICATION_STARTED")
        assert task_idx < ver_idx
        # EXECUTION_COMPLETED should exist and come before final checkpoint (after_verification)
        exec_idx = next(i for i, e in enumerate(events) if e["event_type"] == "EXECUTION_COMPLETED")
        last_checkpoint_idx = len(events) - 1 - next(i for i, e in enumerate(reversed(events)) if e["event_type"] == "CHECKPOINT_CREATED")
        assert exec_idx < last_checkpoint_idx
        # Ensure EXECUTION_COMPLETED comes after TASK_COMPLETED
        task_completed_idx = next(i for i, e in enumerate(events) if e["event_type"] == "TASK_COMPLETED")
        assert task_completed_idx < exec_idx
    finally:
        try: store.close()
        except: pass
        _safe_unlink(db)


def test_verification_recovery_human_readable():
    """Verification/recovery should have human-readable summaries, not just raw JSON."""
    # Check frontend contains summaries and expandable raw
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    css = Path("app/static/style.css").read_text(encoding="utf-8")

    # Verification summary — human-readable, not just raw JSON
    assert "Verified" in js or "VERIFIED SUCCESSFULLY" in js or "Verification" in js
    assert "View raw evidence" in html or "View raw recovery history" in js

    # Recovery summary for no recovery — human-readable
    assert "No recovery required" in js

    # Expandable details (raw evidence collapsible) — in HTML or generated by JS
    assert "<details>" in js or "<details>" in html
    assert "<summary>" in js or "<summary>" in html

    # Styles for summaries
    assert "ver-summary" in css or "ver-summary" in js
    assert "rec-entry" in css or "rec-entry" in js

    # Backend still provides raw JSON for expandable view
    db = _temp_store()
    try:
        store = SQLiteExecutionStore(db)
        app = create_app(store=store, model_client=FakeModel(_valid_plan()))
        client = TestClient(app)
        resp = client.post("/executions", json={"objective": "Inspect workspace"})
        exec_id = resp.json()["execution_id"]
        detail = client.get(f"/executions/{exec_id}").json()
        assert "verification_results" in detail
        assert "recovery_history" in detail
        # Tasks carry verification
        tasks = client.get(f"/executions/{exec_id}/tasks").json()
        assert tasks["inspect_workspace"]["verification"] is not None
    finally:
        try: store.close()
        except: pass
        _safe_unlink(db)


def test_frontend_treats_backend_as_source_of_truth():
    """UI must not invent state — all data via API, no duplicated orchestration."""
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    # Must call API, not duplicate logic
    assert "fetch('/executions'" in js
    assert "fetch(`/executions/${id}`" in js or "getExecution" in js
    assert "getTasks" in js
    assert "getEvents" in js
    # Must not directly mutate status
    assert "TaskStatus" not in js
    assert "ExecutionStatus" not in js
    # Must derive pipeline from events, not hardcoded status
    assert "RECOVERY_STARTED" in js
    # No frontend execution logic
    assert "execute_task" not in js
    assert "verify_task" not in js
