"""
Deployment/build regression guard.

Prevents the production image from silently losing the capability to execute
the real test suite. Regression history:

- 2026-08-25: Cloud Run returned NO_TEST_SUITE_FOUND for "Run my test suite"
  because .dockerignore excluded tests/ and the Dockerfile never COPY'd it,
  and uv sync --no-dev omitted pytest from the runtime image.

Two layers:

1. BUILD-TIME checks parse Dockerfile/.dockerignore. They run where those
   files exist (repo checkout) and skip inside the built image.
2. RUNTIME checks execute everywhere, including inside the Cloud Run
   container: the suite must be discoverable and pytest importable.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Inside the production image there is no Dockerfile at the repo root.
_BUILD_FILES_PRESENT = (REPO_ROOT / "Dockerfile").is_file() and (
    REPO_ROOT / ".dockerignore"
).is_file()


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Layer 1 — build-time guards (repo checkout only)
# ---------------------------------------------------------------------------

def test_dockerignore_does_not_exclude_tests():
    """tests/ must reach the Docker build context."""
    if not _BUILD_FILES_PRESENT:
        import pytest

        pytest.skip("Running inside built image — no .dockerignore at repo root")
    content = _read(".dockerignore")
    lines = [ln.strip() for ln in content.splitlines()]
    bare = [ln for ln in lines if ln and not ln.startswith("#")]
    assert "tests" not in bare, (
        ".dockerignore excludes 'tests' — run_test_suite cannot discover "
        "the real suite in production images."
    )


def test_dockerfile_copies_tests_into_image():
    if not _BUILD_FILES_PRESENT:
        import pytest

        pytest.skip("Running inside built image — no Dockerfile at repo root")
    dockerfile = _read("Dockerfile")
    assert "COPY tests ./tests" in dockerfile, (
        "Dockerfile must 'COPY tests ./tests' so /app/tests exists at runtime."
    )


def test_dockerfile_installs_pytest_at_runtime():
    """uv sync must NOT use --no-dev: pytest is a dev-group dependency but a
    runtime requirement for run_test_suite inside the container."""
    if not _BUILD_FILES_PRESENT:
        import pytest

        pytest.skip("Running inside built image — no Dockerfile at repo root")
    dockerfile = _read("Dockerfile")
    assert "--no-dev" not in dockerfile, (
        "uv sync --no-dev omits pytest; run_test_suite would report "
        "PytestNotAvailable in production."
    )
    assert "uv sync --frozen" in dockerfile


def test_pyproject_declares_pytest():
    content = _read("pyproject.toml")
    assert "pytest" in content, "pytest must remain declared as a dependency"


def test_dockerignore_does_not_exclude_demo():
    """demo/workloads must reach the Docker build context."""
    if not _BUILD_FILES_PRESENT:
        import pytest

        pytest.skip("Running inside built image — no .dockerignore at repo root")
    content = _read(".dockerignore")
    lines = [ln.strip() for ln in content.splitlines()]
    bare = [ln for ln in lines if ln and not ln.startswith("#")]
    assert "demo" not in bare, (
        ".dockerignore excludes 'demo' — demo/workloads/golden_demo.csv would be missing in production."
    )


def test_dockerfile_copies_demo_workload():
    if not _BUILD_FILES_PRESENT:
        import pytest

        pytest.skip("Running inside built image — no Dockerfile at repo root")
    dockerfile = _read("Dockerfile")
    assert "COPY demo ./demo" in dockerfile, (
        "Dockerfile must 'COPY demo ./demo' so /app/demo/workloads/golden_demo.csv exists at runtime."
    )


# ---------------------------------------------------------------------------
# Layer 2 — runtime guards (must pass inside the Cloud Run container too)
# ---------------------------------------------------------------------------

def test_real_test_suite_is_discoverable_from_workspace_root():
    """THE production guarantee: cwd-relative tests/ contains real test files."""
    tests_dir = REPO_ROOT / "tests"
    assert tests_dir.is_dir(), "tests/ directory missing from workspace root"
    test_files = [f for f in tests_dir.rglob("test_*.py") if f.is_file()]
    assert len(test_files) >= 10, "Expected a substantial real test suite in tests/"


def test_pytest_importable_in_this_environment():
    """Inside the image this proves the dev group was installed (no --no-dev)."""
    import importlib.util

    assert importlib.util.find_spec("pytest") is not None, (
        "pytest not importable — run_test_suite cannot execute in this "
        "environment."
    )


def test_demo_workload_csv_discoverable_at_runtime():
    """demo/workloads/golden_demo.csv must be present in both local and container."""
    from app.workload.golden_demo import CANONICAL_DEMO_CSV

    assert CANONICAL_DEMO_CSV.is_file(), f"Demo CSV not found at {CANONICAL_DEMO_CSV}"
    content = CANONICAL_DEMO_CSV.read_text(encoding="utf-8-sig")
    assert "TaskID" in content and "What Kernorq should do" in content


def test_demo_workload_loads_regardless_of_cwd():
    """Workload loader must work even when cwd is not the repo root (cloud: /app vs local)."""
    import os
    import tempfile

    from app.workload.golden_demo import load_golden_demo_tasks

    original_cwd = os.getcwd()
    tmpdir = tempfile.mkdtemp()
    try:
        os.chdir(tmpdir)
        tasks = load_golden_demo_tasks()
        assert len(tasks) == 22, "Golden demo should always have 22 tasks"
        assert all(t.id for t in tasks)
    finally:
        os.chdir(original_cwd)
        # Cleanup temp dir (Windows may hold lock briefly)
        import shutil
        import time

        for _ in range(3):
            try:
                shutil.rmtree(tmpdir, ignore_errors=False)
                break
            except PermissionError:
                time.sleep(0.1)


def test_demo_workload_via_explicit_relative_path():
    """Portable relative path demo/workloads/golden_demo.csv must remain valid."""
    from pathlib import Path

    from app.workload.golden_demo import load_golden_demo_tasks

    # This is the portable path the spec requires to keep working
    tasks = load_golden_demo_tasks(Path("demo/workloads/golden_demo.csv"))
    assert len(tasks) == 22
