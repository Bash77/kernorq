from __future__ import annotations

import os
import tempfile
from pathlib import Path
import pytest
from app.tools import inspect_project_workspace
from app.agent import (
    root_agent,
    MODEL_NAME,
    AGENT_INSTRUCTION,
    validate_and_get_model_name,
    MINIMUM_MODEL_VERSION,
)
from app import app


def test_inspect_project_workspace_structure():
    result = inspect_project_workspace(".")
    assert result["success"] is True
    assert "repository_root" in result
    assert isinstance(result["files"], list)
    assert isinstance(result["directories"], list)
    assert "checks" in result
    checks = result["checks"]
    assert "readme_exists" in checks
    assert "pyproject_exists" in checks
    assert "tests_directory_exists" in checks
    assert "python_test_file_count" in checks
    assert result["error"] is None


def test_inspect_project_workspace_read_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / "README.md").write_text("# Test", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'", encoding="utf-8")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_sample.py").write_text("def test_ok(): pass", encoding="utf-8")

        initial_files = list(tmp_path.iterdir())
        result = inspect_project_workspace(str(tmp_path))

        assert result["success"] is True
        assert result["checks"]["readme_exists"] is True
        assert result["checks"]["pyproject_exists"] is True
        assert result["checks"]["tests_directory_exists"] is True
        assert result["checks"]["python_test_file_count"] == 1

        after_files = list(tmp_path.iterdir())
        assert [f.name for f in initial_files] == [f.name for f in after_files]


def test_inspect_project_workspace_nonexistent_directory():
    result = inspect_project_workspace("./non_existent_directory_12345")
    assert result["success"] is False
    assert result["error"]["type"] == "DirectoryNotFoundError"


def test_root_agent_properties():
    assert root_agent.name == "root_agent"
    assert root_agent.model == MODEL_NAME
    assert root_agent.instruction == AGENT_INSTRUCTION
    assert len(root_agent.tools) == 1
    assert root_agent.tools[0] == inspect_project_workspace


def test_adk_app_structure():
    assert app.name == "app"
    assert app.root_agent == root_agent


def test_model_compliance():
    # Verify the currently configured production model is Gemini 3.5+
    assert MODEL_NAME == "gemini-3.5-flash"
    assert root_agent.model == "gemini-3.5-flash"
    assert "2.5" not in MODEL_NAME


def test_model_validation_rejects_sub_3_5(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "gemini-2.5-flash")
    with pytest.raises(ValueError, match="not compliant"):
        validate_and_get_model_name()

    monkeypatch.setenv("MODEL_NAME", "gemini-2.0-flash")
    with pytest.raises(ValueError, match="not compliant"):
        validate_and_get_model_name()

    monkeypatch.setenv("MODEL_NAME", "gemini-1.5-pro")
    with pytest.raises(ValueError, match="not compliant"):
        validate_and_get_model_name()


def test_model_validation_accepts_compliant_models(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "gemini-3.5-flash")
    assert validate_and_get_model_name() == "gemini-3.5-flash"

    monkeypatch.setenv("MODEL_NAME", "gemini-3.6-flash")
    assert validate_and_get_model_name() == "gemini-3.6-flash"

    monkeypatch.setenv("MODEL_NAME", "gemini-3.7-flash")
    assert validate_and_get_model_name() == "gemini-3.7-flash"

