from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path
from typing import Any

_SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", ".pytest_cache", "node_modules", ".agents", "dist", "build", ".idea", ".vscode"}
_MAX_FILES = 800


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        files.append(p)
        if len(files) >= _MAX_FILES:
            break
    return sorted(files)


def _check_source_structure(target: Path, workspace_root: Path) -> tuple[list[dict], list[str]]:
    issues: list[dict] = []
    warnings: list[str] = []
    if target == workspace_root:
        if not (target / "app").is_dir():
            issues.append({"check": "source_structure", "severity": "issue", "message": "Missing 'app' package at repository root"})
        elif not (target / "app" / "api" / "main.py").is_file():
            issues.append({"check": "source_structure", "severity": "issue", "message": "Missing app/api/main.py entrypoint"})
    else:
        py_count = len(_iter_python_files(target))
        if py_count == 0:
            warnings.append(f"No Python source files found under '{target.name}'")
    return issues, warnings


def _check_python_syntax(files: list[Path]) -> tuple[list[dict], int]:
    issues: list[dict] = []
    inspected = 0
    for f in files:
        inspected += 1
        try:
            source = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            issues.append({"check": "python_syntax", "severity": "issue", "file": str(f), "message": f"Unreadable file: {exc}"})
            continue
        try:
            compile(source, str(f), "exec")
        except SyntaxError as exc:
            issues.append({
                "check": "python_syntax",
                "severity": "issue",
                "file": str(f),
                "line": exc.lineno,
                "message": f"SyntaxError: {exc.msg}",
            })
    return issues, inspected


def _resolve_internal(import_name: str, base: Path) -> bool:
    rel = Path(*import_name.split("."))
    return (base / rel.with_suffix(".py")).is_file() or (base / rel / "__init__.py").is_file()


def _check_imports(files: list[Path], target: Path) -> tuple[list[dict], int]:
    issues: list[dict] = []
    checked = 0
    src_base = target / "app" if (target / "app").is_dir() else target
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue  # already reported by syntax check
        checked += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                if node.module and not _resolve_internal(node.module, f.parent):
                    issues.append({
                        "check": "import_resolution",
                        "severity": "issue",
                        "file": str(f),
                        "line": node.lineno,
                        "message": f"Relative import '.{node.module}' does not resolve",
                    })
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top == "app" and not _resolve_internal(node.module, src_base.parent):
                    issues.append({
                        "check": "import_resolution",
                        "severity": "issue",
                        "file": str(f),
                        "line": node.lineno,
                        "message": f"Absolute import 'app.{node.module}' does not resolve inside project",
                    })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top == "app" and not _resolve_internal(alias.name, src_base.parent):
                        issues.append({
                            "check": "import_resolution",
                            "severity": "issue",
                            "file": str(f),
                            "line": node.lineno,
                            "message": f"Absolute import '{alias.name}' does not resolve inside project",
                        })
    return issues, checked


def _check_config(target: Path, workspace_root: Path) -> tuple[list[dict], list[str]]:
    issues: list[dict] = []
    warnings: list[str] = []
    pyproject = target / "pyproject.toml"
    if not pyproject.is_file():
        if target == workspace_root:
            issues.append({"check": "config_files", "severity": "issue", "message": "Missing pyproject.toml at repository root"})
        else:
            warnings.append("No pyproject.toml in diagnostics scope")
        return issues, warnings
    try:
        with open(pyproject, "rb") as fh:
            tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        issues.append({"check": "config_files", "severity": "issue", "message": f"pyproject.toml is invalid TOML: {exc}"})
    if target == workspace_root:
        for optional in ("README.md", ".env.example", ".gitignore"):
            if not (target / optional).is_file():
                warnings.append(f"Recommended file missing: {optional}")
    return issues, warnings


def _check_dependencies(target: Path, workspace_root: Path) -> tuple[list[dict], list[str]]:
    issues: list[dict] = []
    warnings: list[str] = []
    pyproject = target / "pyproject.toml"
    if not pyproject.is_file():
        return issues, warnings
    try:
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return issues, warnings  # already reported by config check
    deps = data.get("project", {}).get("dependencies", [])
    lock = target / "uv.lock"
    if target == workspace_root and deps:
        if not lock.is_file():
            warnings.append("Declared dependencies exist but uv.lock is missing")
        else:
            lock_text = lock.read_text(encoding="utf-8", errors="replace").lower()
            for dep in deps:
                name = dep.split(";")[0].split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().replace("_", "-").lower()
                if name and name.replace("-", "_") not in lock_text.replace("-", "_") and name not in lock_text:
                    warnings.append(f"Dependency '{name}' not found in uv.lock")
    return issues, warnings


def _check_tests(target: Path) -> tuple[list[str], int]:
    warnings: list[str] = []
    tests_dir = target / "tests"
    if not tests_dir.is_dir():
        warnings.append("No tests/ directory found — test suite unavailable")
        return warnings, 0
    test_files = [f for f in tests_dir.rglob("test_*.py") if f.is_file()]
    if not test_files:
        warnings.append("tests/ exists but contains no test_*.py files")
    return warnings, len(test_files)


def project_diagnostics(directory_path: str = ".") -> dict[str, Any]:
    """Performs read-only diagnostics on the project and returns structured findings.

    Checks: source structure, Python syntax, internal import resolution,
    configuration files, dependency lock consistency, and test availability.

    Args:
        directory_path: Repository-relative directory to diagnose. Defaults
            to the workspace root. Must resolve inside the workspace.

    Returns:
        Structured dict with ``success``, ``issues``, ``warnings``,
        ``checks_run``, ``files_inspected``, ``status`` (``clean`` |
        ``issues_found`` | ``error``) plus summary counts. Finding issues is
        a successful diagnostic run; ``success`` is False only when the tool
        itself failed (status ``error``).
    """
    try:
        workspace_root = Path(".").resolve()
        raw_target = Path(directory_path) if directory_path else Path(".")
        target = raw_target.resolve() if raw_target.is_absolute() else (workspace_root / raw_target).resolve()
        try:
            is_inside = target.is_relative_to(workspace_root)
        except AttributeError:
            is_inside = str(target).startswith(str(workspace_root))
        if not is_inside:
            return {
                "success": False,
                "status": "error",
                "error": {"type": "InvalidPathError", "message": f"directory_path must be inside workspace: {directory_path}"},
                "issues": [], "warnings": [], "checks_run": [], "files_inspected": 0,
            }
        if not target.is_dir():
            return {
                "success": False,
                "status": "error",
                "error": {"type": "DirectoryNotFoundError", "message": f"Directory does not exist: {directory_path}"},
                "issues": [], "warnings": [], "checks_run": [], "files_inspected": 0,
            }

        checks_run: list[str] = []
        all_issues: list[dict] = []
        all_warnings: list[str] = []

        checks_run.append("source_structure")
        s_issues, s_warn = _check_source_structure(target, workspace_root)
        all_issues += s_issues
        all_warnings += s_warn

        files = _iter_python_files(target)

        checks_run.append("python_syntax")
        syn_issues, files_inspected = _check_python_syntax(files)
        all_issues += syn_issues

        checks_run.append("import_resolution")
        imp_issues, _ = _check_imports(files, target)
        all_issues += imp_issues

        checks_run.append("config_files")
        cfg_issues, cfg_warn = _check_config(target, workspace_root)
        all_issues += cfg_issues
        all_warnings += cfg_warn

        checks_run.append("dependency_lock")
        dep_issues, dep_warn = _check_dependencies(target, workspace_root)
        all_issues += dep_issues
        all_warnings += dep_warn

        checks_run.append("test_suite_availability")
        t_warn, test_count = _check_tests(target)
        all_warnings += t_warn

        status = "issues_found" if all_issues else "clean"
        return {
            "success": True,
            "status": status,
            "issues": all_issues,
            "warnings": all_warnings,
            "checks_run": checks_run,
            "files_inspected": files_inspected,
            "summary": {
                "issue_count": len(all_issues),
                "warning_count": len(all_warnings),
                "test_file_count": test_count,
            },
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "status": "error",
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "issues": [], "warnings": [], "checks_run": [], "files_inspected": 0,
        }
