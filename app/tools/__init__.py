from __future__ import annotations

from pathlib import Path
from typing import Any


def inspect_project_workspace(
    directory_path: str = ".",
) -> dict[str, Any]:
    """Inspects the project workspace directory and returns structural metadata.

    Args:
        directory_path: The directory path to inspect. Defaults to current directory.

    Returns:
        A dictionary containing the inspection result and check flags.
    """
    try:
        target_dir = Path(directory_path).resolve()
        if not target_dir.exists():
            return {
                "success": False,
                "error": {
                    "type": "DirectoryNotFoundError",
                    "message": f"Directory does not exist: {directory_path}",
                },
            }

        if not target_dir.is_dir():
            return {
                "success": False,
                "error": {
                    "type": "NotADirectoryError",
                    "message": f"Path is not a directory: {directory_path}",
                },
            }

        entries = list(target_dir.iterdir())
        files = sorted([e.name for e in entries if e.is_file()])
        directories = sorted([e.name for e in entries if e.is_dir()])

        readme_exists = any(e.name.lower() == "readme.md" for e in entries if e.is_file())
        pyproject_exists = (target_dir / "pyproject.toml").is_file()

        tests_dir = target_dir / "tests"
        tests_dir_exists = tests_dir.is_dir()

        test_file_count = 0
        if tests_dir_exists:
            test_file_count = len(
                [f for f in tests_dir.rglob("*.py") if f.is_file() and f.name.startswith("test_")]
            )

        return {
            "success": True,
            "repository_root": str(target_dir),
            "files": files,
            "directories": directories,
            "checks": {
                "readme_exists": readme_exists,
                "pyproject_exists": pyproject_exists,
                "tests_directory_exists": tests_dir_exists,
                "python_test_file_count": test_file_count,
            },
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }


from app.tools.content import analyze_competitors, generate_carousel, research_topic
from app.tools.project_diagnostics import project_diagnostics
from app.tools.run_test_suite import run_test_suite

from app.tools.registry import (
    DuplicateToolError,
    ToolNotFoundError,
    ToolRegistry,
    create_default_tool_registry,
)

__all__ = [
    "inspect_project_workspace",
    "run_test_suite",
    "project_diagnostics",
    "research_topic",
    "analyze_competitors",
    "generate_carousel",
    "ToolRegistry",
    "DuplicateToolError",
    "ToolNotFoundError",
    "create_default_tool_registry",
]

