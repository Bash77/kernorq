"""
Regression tests for project_diagnostics semantic verification.

Covers:
- tool performs real read-only diagnostics on the repo (structured fields)
- syntax errors in a temp file inside workspace are detected -> issues_found
- verifier: status=issues_found -> VERIFIED_SUCCESS with issue evidence
- verifier: status=clean -> VERIFIED_SUCCESS with clean evidence
- verifier: no checks_run / tool failure -> VERIFIED_FAILURE
- inspect-style payload ({success, files}) for a diagnostics task can NEVER
  produce verified_success — "Check my project for problems" cannot become
  COMPLETED merely because inspect_project_workspace returned success+files
- end-to-end via runner with fake Gemini planning project_diagnostics
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.executor import execute_task
from app.orchestration.state import (
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    VerificationStatus,
)
from app.orchestration.verifier import create_default_strategy_registry, verify_task
from app.tools.project_diagnostics import project_diagnostics
from app.tools.registry import create_default_tool_registry


# ---------------------------------------------------------------------------
# Direct tool tests — real diagnostics on this repository
# ---------------------------------------------------------------------------

def test_diagnostics_performs_real_checks_on_repo():
    result = project_diagnostics(directory_path=".")
    assert result["success"] is True
    assert result["status"] in {"clean", "issues_found"}
    # Structured contract
    assert isinstance(result["issues"], list)
    assert isinstance(result["warnings"], list)
    assert isinstance(result["checks_run"], list) and len(result["checks_run"]) >= 6
    expected_checks = {
        "source_structure", "python_syntax", "import_resolution",
        "config_files", "dependency_lock", "test_suite_availability",
    }
    assert expected_checks.issubset(set(result["checks_run"]))
    assert result["files_inspected"] > 0
    assert result["error"] is None


def test_diagnostics_detects_syntax_error_in_temp_file():
    bad_dir = Path("diagnostics_tmp_bad_777")
    try:
        bad_dir.mkdir(exist_ok=True)
        (bad_dir / "broken.py").write_text("def broken(:\n    pass\n")
        result = project_diagnostics(directory_path=str(bad_dir))
        assert result["status"] == "issues_found"
        assert result["success"] is True  # finding issues is a successful diagnostic run
        syntax_issues = [i for i in result["issues"] if i["check"] == "python_syntax"]
        assert len(syntax_issues) == 1
        assert "SyntaxError" in syntax_issues[0]["message"]
        assert any("broken.py" in str(i.get("file", "")) for i in syntax_issues)
    finally:
        import shutil
        if bad_dir.exists():
            shutil.rmtree(bad_dir, ignore_errors=True)


def test_diagnostics_rejects_path_traversal():
    result = project_diagnostics(directory_path="../../..")
    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error"]["type"] == "InvalidPathError"
    assert result["checks_run"] == []


def test_diagnostics_missing_directory_is_tool_failure_not_clean():
    result = project_diagnostics(directory_path="nonexistent_diag_12345")
    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error"]["type"] == "DirectoryNotFoundError"


# ---------------------------------------------------------------------------
# Semantic verification of diagnostics results
# ---------------------------------------------------------------------------

def _make_verifying_diagnostics_task(result: dict):
    store = InMemoryExecutionStore()
    execution = Execution(execution_id="exec_diag_verify", objective="Check my project for problems")
    task = Task(
        task_id="diagnose_project",
        execution_id="exec_diag_verify",
        title="Diagnose project",
        description="Run read-only diagnostics",
        tool_name="project_diagnostics",
        status=TaskStatus.VERIFYING,
    )
    task.result = result
    execution.add_task(task)
    execution.status = ExecutionStatus.VERIFYING
    store.create_execution(execution)
    return store, execution


def test_verification_success_with_issues_found():
    result = {
        "success": True,
        "status": "issues_found",
        "issues": [{"check": "python_syntax", "severity": "issue", "file": "x.py", "message": "SyntaxError: bad"}],
        "warnings": ["No tests found"],
        "checks_run": ["source_structure", "python_syntax"],
        "files_inspected": 12,
        "error": None,
    }
    store, execution = _make_verifying_diagnostics_task(result)
    updated = verify_task(execution, "diagnose_project", store, create_default_strategy_registry())
    task = updated.tasks["diagnose_project"]
    assert task.status == TaskStatus.SUCCEEDED
    assert task.verification.status == VerificationStatus.VERIFIED_SUCCESS
    assert task.verification.evidence["issue_count"] == 1
    assert "SyntaxError: bad" in task.verification.evidence["issues_preview"]


def test_verification_success_with_clean_evidence():
    result = {
        "success": True,
        "status": "clean",
        "issues": [],
        "warnings": [],
        "checks_run": ["source_structure", "python_syntax", "import_resolution", "config_files", "dependency_lock", "test_suite_availability"],
        "files_inspected": 40,
        "error": None,
    }
    store, execution = _make_verifying_diagnostics_task(result)
    updated = verify_task(execution, "diagnose_project", store, create_default_strategy_registry())
    task = updated.tasks["diagnose_project"]
    assert task.status == TaskStatus.SUCCEEDED
    assert task.verification.status == VerificationStatus.VERIFIED_SUCCESS
    assert task.verification.evidence["explicit_outcome"] == "CLEAN"
    assert len(task.verification.evidence["checks_run"]) == 6


def test_verification_fails_when_no_checks_run():
    # success=true alone is NEVER sufficient
    result = {"success": True, "status": "clean", "issues": [], "warnings": [], "checks_run": [], "files_inspected": 0, "error": None}
    store, execution = _make_verifying_diagnostics_task(result)
    updated = verify_task(execution, "diagnose_project", store, create_default_strategy_registry())
    task = updated.tasks["diagnose_project"]
    assert task.status == TaskStatus.FAILED
    assert task.verification.status == VerificationStatus.VERIFIED_FAILURE
    assert "No diagnostics checks were performed" in task.verification.message


def test_verification_fails_on_tool_failure():
    result = {
        "success": False,
        "status": "error",
        "issues": [], "warnings": [], "checks_run": [], "files_inspected": 0,
        "error": {"type": "DirectoryNotFoundError", "message": "missing"},
    }
    store, execution = _make_verifying_diagnostics_task(result)
    updated = verify_task(execution, "diagnose_project", store, create_default_strategy_registry())
    task = updated.tasks["diagnose_project"]
    assert task.status == TaskStatus.FAILED
    assert task.verification.status == VerificationStatus.VERIFIED_FAILURE


def test_inspect_payload_cannot_verify_diagnostics_objective():
    """
    THE regression: 'Check my project for problems' must not become COMPLETED
    merely because an inspect-shaped result {success: True, files: [...]} is
    present. The diagnostics strategy rejects it.
    """
    inspect_shaped = {
        "success": True,
        "repository_root": "/app",
        "files": ["README.md", "pyproject.toml"],
        "directories": ["app", "tests"],
        "checks": {"readme_exists": True, "pyproject_exists": True, "tests_directory_exists": True, "python_test_file_count": 22},
        "error": None,
    }
    store, execution = _make_verifying_diagnostics_task(inspect_shaped)
    updated = verify_task(execution, "diagnose_project", store, create_default_strategy_registry())
    task = updated.tasks["diagnose_project"]
    assert task.status == TaskStatus.FAILED
    assert task.verification.status == VerificationStatus.VERIFIED_FAILURE
    # Inspect-shaped payload lacks checks_run/status, so it is rejected as
    # "no checks performed" (or unrecognized shape) — never verified_success
    assert (
        "No diagnostics checks were performed" in task.verification.message
        or "not semantically verified" in task.verification.message
    )
    assert "files" in str(task.verification.evidence["result_keys"])


# ---------------------------------------------------------------------------
# Orchestrator / runner end-to-end
# ---------------------------------------------------------------------------

def test_orchestrator_diagnostic_objective_end_to_end():
    """Real plan -> executor runs real diagnostics -> semantic verification."""
    from app.agent.runner import run_objective

    class FakeModel:
        def generate(self, objective: str) -> str:
            import json
            return json.dumps({
                "objective": objective,
                "tasks": [{
                    "task_id": "diagnose_project",
                    "title": "Diagnose project",
                    "description": "Check my project for problems",
                    "tool_name": "project_diagnostics",
                    "tool_input": {"directory_path": "."},
                    "dependencies": [],
                    "max_attempts": 2,
                }],
            })

    exec_result = run_objective(
        "Check my project for problems",
        store=InMemoryExecutionStore(),
        model_client=FakeModel(),
        tool_registry=create_default_tool_registry(),
    )
    task = exec_result.tasks["diagnose_project"]
    assert task.result is not None
    assert len(task.result["checks_run"]) >= 6
    assert task.result["files_inspected"] > 0
    # Either outcome is honest; both require real checks evidence
    if task.result["status"] == "issues_found":
        assert task.verification.evidence["issue_count"] == len(task.result["issues"])
    else:
        assert task.verification.evidence["explicit_outcome"] == "CLEAN"


def test_planner_prompt_lists_diagnostics_tool():
    """The Gemini prompt must expose project_diagnostics so diagnostic objectives select it."""
    from app.agent.gemini_client import GeminiModelClient

    registry = create_default_tool_registry()
    client = GeminiModelClient.__new__(GeminiModelClient)
    client.registry = registry
    allowed = client.registry.list_tools()
    assert "project_diagnostics" in allowed

    # Prompt construction includes diagnostics guidance (build the same f-string source)
    import inspect as pyinspect
    src = pyinspect.getsource(GeminiModelClient.generate)
    assert "project_diagnostics" in src
    assert '"find bugs"' in src or "find bugs" in src


def test_default_registry_contains_all_three_semantic_tools():
    registry = create_default_tool_registry()
    tools = registry.list_tools()
    assert "inspect_project_workspace" in tools
    assert "run_test_suite" in tools
    assert "project_diagnostics" in tools
