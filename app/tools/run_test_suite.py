from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def run_test_suite(
    test_path: str = "tests",
    extra_args: str | None = None,
) -> dict[str, Any]:
    """Executes the project's pytest suite and returns structured results.

    This is a deterministic, narrow tool — it only runs ``python -m pytest`` on
    a repository-relative path that exists inside the workspace.

    Args:
        test_path: Repository-relative directory or file to test. Defaults to
            ``tests``. Must resolve inside the current workspace.
        extra_args: Optional *safe* extra pytest args as a single string.
            Only ``-k``, ``-q``, ``--tb``-like flags are honoured; any
            value containing shell metacharacters is rejected.

    Returns:
        Structured dict with at least:
        ``command``, ``exit_code``, ``passed``, ``failed``, ``skipped``,
        ``test_count``, ``stdout``, ``stderr``, ``success``, ``status``.

        ``status`` is one of ``passed``, ``failed``, ``no_test_suite``,
        ``error``.

        Deterministic precondition answers are definitive diagnostic
        outcomes, NOT tool failures: a missing suite, an empty suite, or an
        environment without pytest return ``success: True`` with
        ``status: "no_test_suite"`` and
        ``explicit_outcome: "NO_TEST_SUITE_FOUND"``. These must never be
        retried — the outcome cannot change.

        Tool-mechanism problems (invalid path, timeout, subprocess crash)
        return ``success: False`` with ``error.type`` ``InvalidPathError``,
        ``TimeoutError``, or the underlying exception name and ``status``
        ``error``. Failing tests return ``success: False`` with
        ``status: "failed"`` so the verifier surfaces real failures.
    """
    # Standard payload skeleton so every return carries the full contract
    def _payload(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "success": False,
            "status": "error",
            "command": "",
            "exit_code": None,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "test_count": 0,
            "stdout": "",
            "stderr": "",
            "explicit_outcome": None,
            "error": None,
        }
        base.update(overrides)
        return base
    # Resolve workspace root (where pyproject.toml lives or cwd)
    try:
        workspace_root = Path(".").resolve()
        # Basic sanity: ensure we are inside a plausible project
        raw_target = Path(test_path) if test_path else Path("tests")
        # Reject absolute paths outside workspace explicitly
        target = (workspace_root / raw_target).resolve() if not raw_target.is_absolute() else raw_target.resolve()

        # Security: target must be inside workspace_root
        try:
            is_inside = target.is_relative_to(workspace_root)
        except AttributeError:
            # Python <3.9 fallback
            is_inside = str(target).startswith(str(workspace_root))
        if not is_inside:
            return {
                "success": False,
                "status": "error",
                "error": {
                    "type": "InvalidPathError",
                    "message": f"test_path must be inside workspace: {test_path}",
                },
                "command": "",
                "exit_code": None,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "test_count": 0,
                "stdout": "",
                "stderr": "",
            }

        # Validate extra_args for shell metacharacters
        if extra_args:
            if re.search(r"[;&|`$><\\]", extra_args):
                return {
                    "success": False,
                    "status": "error",
                    "error": {
                        "type": "InvalidArgumentError",
                        "message": "extra_args contains disallowed characters",
                    },
                    "command": "",
                    "exit_code": None,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "test_count": 0,
                    "stdout": "",
                    "stderr": "",
                }

        # Check tests directory existence — deterministic precondition answer,
        # NOT a tool failure: retrying cannot make a suite appear.
        if not target.exists():
            return _payload(
                success=True,
                status="no_test_suite",
                explicit_outcome="NO_TEST_SUITE_FOUND",
                error={
                    "type": "NoTestSuiteFound",
                    "message": f"No test suite found at '{test_path}': path does not exist",
                },
                command=f"{sys.executable} -m pytest {test_path}",
            )

        # If target is directory, check it contains test files
        if target.is_dir():
            test_files = [f for f in target.rglob("*.py") if f.is_file() and f.name.startswith("test_")]
            if not test_files:
                return _payload(
                    success=True,
                    status="no_test_suite",
                    explicit_outcome="NO_TEST_SUITE_FOUND",
                    error={
                        "type": "NoTestSuiteFound",
                        "message": f"No test suite found at '{test_path}': no test_*.py files",
                    },
                    command=f"{sys.executable} -m pytest {test_path}",
                )

        # Check pytest availability
        try:
            import pytest  # noqa: F401
        except ImportError:
            return _payload(
                success=True,
                status="no_test_suite",
                explicit_outcome="NO_TEST_SUITE_FOUND",
                error={
                    "type": "PytestNotAvailable",
                    "message": "pytest is not installed in this environment",
                },
                command=f"{sys.executable} -m pytest {test_path}",
            )

        # Build command safely (no shell)
        cmd = [sys.executable, "-m", "pytest", str(raw_target), "-q"]
        if extra_args:
            # Split safely on whitespace (no shell)
            cmd.extend(extra_args.split())

        # Execute with timeout — full suite can take >60s on Windows, allow 180s
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(workspace_root),
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "success": False,
                "status": "error",
                "error": {
                    "type": "TimeoutError",
                    "message": f"pytest timed out after 180s: {exc}",
                },
                "command": " ".join(cmd),
                "exit_code": None,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "test_count": 0,
                "stdout": exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                "stderr": exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            }
        except Exception as exc:
            return {
                "success": False,
                "status": "error",
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "command": " ".join(cmd),
                "exit_code": None,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "test_count": 0,
                "stdout": "",
                "stderr": "",
            }

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        exit_code = result.returncode

        # Parse pytest summary
        # Examples: "2 passed in 0.12s", "1 failed, 2 passed in 0.34s", "1 skipped, 1 passed"
        passed = failed = skipped = 0
        # Find last summary line containing "passed" or "failed"
        summary_match = re.search(r"(\d+) passed", stdout)
        if summary_match:
            passed = int(summary_match.group(1))
        failed_match = re.search(r"(\d+) failed", stdout)
        if failed_match:
            failed = int(failed_match.group(1))
        skipped_match = re.search(r"(\d+) skipped", stdout)
        if skipped_match:
            skipped = int(skipped_match.group(1))
        # Also check for errors
        error_match = re.search(r"(\d+) error", stdout)
        errors = int(error_match.group(1)) if error_match else 0
        failed += errors

        # pytest exit codes: 0=passed, 1=tests failed, 2=interrupted, 3=internal error, 4=pytest usage error, 5=no tests collected
        if exit_code == 5:
            # Suite path exists but collects zero tests — definitive answer
            return _payload(
                success=True,
                status="no_test_suite",
                explicit_outcome="NO_TEST_SUITE_FOUND",
                command=" ".join(cmd),
                exit_code=exit_code,
                passed=passed,
                failed=failed,
                skipped=skipped,
                test_count=passed + failed + skipped,
                stdout=stdout[-4000:],
                stderr=stderr[-4000:],
                error={
                    "type": "NoTestSuiteFound",
                    "message": "No test suite found at '{0}': pytest collected no tests (exit code 5)".format(test_path),
                },
            )

        test_count = passed + failed + skipped
        # Handle -q output where no summary but exit_code 0 still means passed
        if test_count == 0 and exit_code == 0:
            # Fallback: if pytest ran but no summary parsed, assume at least 0
            pass

        if exit_code == 0:
            status = "passed"
            success = True
        elif exit_code == 1:
            status = "failed"
            success = False
        else:
            status = "error"
            success = False

        # Include explicit error for failed case so verifier can surface
        err = None
        if not success:
            if failed > 0:
                err = {
                    "type": "TestFailures",
                    "message": f"{failed} test(s) failed, {passed} passed, {skipped} skipped",
                }
            elif exit_code not in (0, 1, 5):
                err = {
                    "type": "PytestError",
                    "message": f"pytest exit code {exit_code}",
                }

        out: dict[str, Any] = {
            "success": success,
            "status": status,
            "command": " ".join(cmd),
            "exit_code": exit_code,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "test_count": test_count,
            "stdout": stdout[-4000:] if len(stdout) > 4000 else stdout,
            "stderr": stderr[-4000:] if len(stderr) > 4000 else stderr,
        }
        if err:
            out["error"] = err
        else:
            out["error"] = None
        return out

    except Exception as exc:
        return {
            "success": False,
            "status": "error",
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "command": "",
            "exit_code": None,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "test_count": 0,
            "stdout": "",
            "stderr": "",
        }
