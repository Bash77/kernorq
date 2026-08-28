"""
Golden demo CSV integration tests — demo asset integration.

Proves the canonical demo/workloads/golden_demo.csv flows through the REAL
execution path: explicit field mapping -> Phase 1 validation -> Phase 2
workload manager -> existing orchestrator/executor/verifier/recovery — with
the ACTUAL invocation order asserted via call-logging tools.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.state import ExecutionStatus, TaskStatus
from app.tools.registry import ToolRegistry, create_default_tool_registry
from app.workload import WorkloadTask
from app.workload.golden_demo import (
    CANONICAL_DEMO_CSV,
    DEMO_TOOL_MAPPING,
    load_golden_demo_tasks,
    normalize_priority_label,
    run_golden_demo,
    workload_ui_summary,
)
from app.workload.manager import run_workload


def _logging_registry():
    """Registry with call-recording versions of the demo tools.

    Records BOTH the tool name and the executor-injected operation_id, so
    tests can reconstruct exactly WHICH task executed in what order even
    though many tasks share the same tool.
    """
    calls: list[str] = []          # tool names, in invocation order
    call_op_ids: list[str] = []    # operation_ids, in invocation order
    registry = ToolRegistry()

    def _make(tool_name: str):
        def _tool(operation_id: str | None = None, **_kw):
            calls.append(tool_name)
            call_op_ids.append(operation_id or "")
            if tool_name == "research_topic":
                return {
                    "success": True,
                    "topic": _kw.get("topic", "test"),
                    "findings": [{"title": "Finding 1", "detail": "Detail 1"}],
                    "sources": [{"title": "Source 1", "relevance": "Relevant"}],
                    "summary": "Summary",
                    "error": None,
                }
            if tool_name == "analyze_competitors":
                return {
                    "success": True,
                    "topic": _kw.get("topic", "test"),
                    "competitors": [{"company": "Comp A", "website": "a.com", "positioning": "Pos", "hero_message": "Hero", "cta": "CTA", "key_pattern": "Pattern", "strength": "S", "weakness": "W"}],
                    "patterns": [{"title": "Pattern 1", "detail": "Detail"}],
                    "recommendations": ["Rec 1"],
                    "error": None,
                }
            if tool_name == "generate_carousel":
                return {
                    "success": True,
                    "topic": _kw.get("topic", "test"),
                    "hook": "Hook",
                    "slides": [{"title": "Hook", "copy": "Copy"}]*5,
                    "cta": "CTA",
                    "caption": "Caption",
                    "visual_notes": "Notes",
                    "error": None,
                }
            if tool_name == "run_test_suite":
                # Deterministic result without spawning nested pytest
                return {
                    "success": True,
                    "status": "passed",
                    "command": "python -m pytest tests -q",
                    "exit_code": 0,
                    "passed": 5,
                    "failed": 0,
                    "skipped": 0,
                    "test_count": 5,
                    "stdout": "5 passed",
                    "stderr": "",
                    "error": None,
                }
            if tool_name == "project_diagnostics":
                return {
                    "success": True,
                    "status": "clean",
                    "issues": [],
                    "warnings": [],
                    "checks_run": ["source_structure", "python_syntax", "import_resolution",
                                    "config_files", "dependency_lock", "test_suite_availability"],
                    "files_inspected": 10,
                    "summary": {"issue_count": 0, "warning_count": 0, "test_file_count": 22},
                    "error": None,
                }
            return {
                "success": True,
                "repository_root": "/demo",
                "files": ["README.md"],
                "directories": ["app"],
                "checks": {"readme_exists": True},
                "error": None,
            }

        return _tool

    for name in ("inspect_project_workspace", "run_test_suite", "project_diagnostics", "research_topic", "analyze_competitors", "generate_carousel"):
        registry.register(name, _make(name))
    return calls, call_op_ids, registry


# ---------------------------------------------------------------------------
# Asset + mapping
# ---------------------------------------------------------------------------

def test_canonical_csv_exists_in_repo_and_is_portable():
    assert CANONICAL_DEMO_CSV.is_file(), "demo/workloads/golden_demo.csv missing"
    text = CANONICAL_DEMO_CSV.read_text(encoding="utf-8-sig")
    assert "TaskID" in text and "What Kernorq should do" in text
    assert "C:\\" not in text  # no user-specific paths inside the asset


def test_golden_csv_loads_all_22_tasks_with_exact_fields():
    tasks = load_golden_demo_tasks()
    assert len(tasks) == 22
    by_id = {t.id: t for t in tasks}
    assert set(by_id) == {str(i) for i in range(1, 23)}

    t1 = by_id["1"]
    assert t1.title == "Finish Database Systems homework"
    assert t1.task_type == "🎓 University"
    assert t1.deadline == date(2026, 8, 27)
    assert t1.priority == 1          # 🔴 High -> 1
    assert t1.description == "Complete exercises, run tests, report failures"

    t13 = by_id["13"]
    assert t13.title == "Plan weekly workload"
    assert t13.priority == 1
    assert t13.deadline == date(2026, 8, 26)

    assert by_id["7"].priority == 3   # 🟡 Medium -> 3
    assert by_id["22"].priority == 5  # 🟢 Low -> 5
    # Real dependencies from the canonical CSV (three chains)
    assert by_id["14"].dependencies == ["13"]
    assert by_id["1"].dependencies == ["14"]
    assert by_id["16"].dependencies == ["1"]
    assert by_id["4"].dependencies == ["3"]
    assert by_id["17"].dependencies == ["4"]
    assert by_id["18"].dependencies == ["17"]
    assert by_id["12"].dependencies == ["8"]
    assert by_id["9"].dependencies == ["12"]
    assert by_id["10"].dependencies == ["9"]
    assert by_id["11"].dependencies == ["10"]
    # Others remain dependency-free
    assert by_id["2"].dependencies == []
    assert by_id["13"].dependencies == []


def test_priority_label_mapping_is_explicit_and_strict():
    assert normalize_priority_label("🔴 High") == "1"
    assert normalize_priority_label("🟡 Medium") == "3"
    assert normalize_priority_label("🟢 Low") == "5"
    assert normalize_priority_label("high") == "1"
    with pytest.raises(ValueError, match="unknown priority label"):
        normalize_priority_label("🔵 Critical")


def test_demo_tool_mapping_targets_registered_tools():
    registry = create_default_tool_registry()
    for category, tool in DEMO_TOOL_MAPPING.items():
        assert registry.has(tool), f"{category} maps to unregistered tool {tool}"


def test_adapter_per_tool_inputs_regression():
    """Regression: tools with distinct signatures get their own kwargs
    (run_test_suite takes test_path; inspect takes directory_path)."""
    from app.workload import build_workload_plan
    from app.workload.adapter import to_execution_plan_dict

    registry = create_default_tool_registry()
    tasks = [
        WorkloadTask(id="A", title="inspect", task_type="💻 Project"),
        WorkloadTask(id="B", title="test", task_type="🤖 Kernorq Demo"),
        WorkloadTask(id="C", title="generic"),
    ]
    plan = build_workload_plan(tasks)
    plan_dict = to_execution_plan_dict(
        tasks, plan, registry, objective="x",
        tool_mapping=dict(DEMO_TOOL_MAPPING),
        default_tool_input={"directory_path": "."},
        tool_inputs={"run_test_suite": {"test_path": "tests"}},
    )
    inputs = {t["task_id"]: t["tool_input"] for t in plan_dict["tasks"]}
    assert inputs["A"] == {"directory_path": "."}
    assert inputs["B"] == {"test_path": "tests"}
    assert inputs["C"] == {"directory_path": "."}


# ---------------------------------------------------------------------------
# Real execution path
# ---------------------------------------------------------------------------

EXPECTED_POLICY_ORDER = [
    "13", "3", "6", "4", "17", "18", "5", "2",
    "14", "1", "16", "7", "15", "8", "12", "9", "10", "11",
    "19", "20", "21", "22",
]


def _op_id_sequence(execution, task_ids: list[str]) -> list[str]:
    """Maps a task-id order to the operation_id sequence the executor injects."""
    return [execution.tasks[tid].operation_id for tid in task_ids]


def test_full_golden_demo_executes_through_existing_pipeline_in_policy_order():
    calls, call_op_ids, registry = _logging_registry()
    result = run_golden_demo(tool_registry=registry)
    execution = result.execution

    assert execution.status == ExecutionStatus.COMPLETED
    assert all(t.status == TaskStatus.SUCCEEDED for t in execution.tasks.values())
    assert len(calls) == 22

    # Deterministic policy order: priority ASC, deadline ASC, id ASC
    assert result.planned_order == EXPECTED_POLICY_ORDER
    # ACTUAL executor invocation order (per-task, via injected operation ids)
    assert call_op_ids == _op_id_sequence(execution, EXPECTED_POLICY_ORDER)

    # Existing verifier produced evidence for every task
    assert len(execution.verification_results) == 22


def test_high_priority_blocked_task_waits_for_dependency():
    """Synthetic extension proves dependency semantics on top of the real CSV:
    a top-priority task depending on '13' stays BLOCKED until 13 succeeds."""
    calls, call_op_ids, registry = _logging_registry()
    tasks = load_golden_demo_tasks()
    tasks.append(WorkloadTask(
        id="VIP_AFTER_13",
        title="Blocked despite highest priority",
        priority=1,
        deadline=date(2026, 8, 25),  # earliest deadline too — still must wait
        dependencies=["13"],
        task_type="💻 Project",
    ))

    result = run_workload(
        tasks,
        objective="golden demo + blocked VIP",
        store=InMemoryExecutionStore(),
        tool_registry=registry,
        tool_mapping=dict(DEMO_TOOL_MAPPING),
        default_tool_input={"directory_path": "."},
    )

    assert result.execution.status == ExecutionStatus.COMPLETED
    planned = result.planned_order
    assert planned[0] == "13"                              # dep first...
    assert planned.index("VIP_AFTER_13") > planned.index("13")
    # ACTUAL runtime obeys: operation-id sequence equals planned sequence
    assert call_op_ids == _op_id_sequence(result.execution, planned)


@pytest.fixture(scope="module")
def demo_result():
    """One shared golden-demo run for read-only facet assertions."""
    _, _, registry = _logging_registry()
    return run_golden_demo(tool_registry=registry)


def test_existing_verifier_and_recovery_pipeline_used_for_demo_tools(demo_result):
    result = demo_result
    assert len(result.execution.verification_results) == 22
    assert all(v.is_success() for v in result.execution.verification_results)
    # Recovery machinery untouched and unused on the happy path
    assert result.execution.recovery_history == []


def test_ui_summary_buckets_and_task_views(demo_result):
    summary = workload_ui_summary(demo_result)

    assert summary["total"] == 22
    assert summary["counts"]["COMPLETED"] == 22
    assert summary["counts"]["FAILED"] == 0
    first = summary["tasks"][0]
    assert first["task_id"] == "13"
    assert first["selection_rank"] == 1
    assert first["priority"] == 1
    assert first["deadline"] == "2026-08-26"
    assert first["status"] == "COMPLETED"
    assert first["why"]  # plan entry reason present for UI display
    assert {"task_id", "title", "priority", "deadline", "dependencies",
            "status", "why", "selection_rank"} <= set(first.keys())


def test_run_script_entry_point_exists_and_is_portable():
    script = CANONICAL_DEMO_CSV.parents[2] / "scripts" / "run_demo_workload.py"
    assert script.is_file()
    content = script.read_text(encoding="utf-8")
    assert "run_golden_demo" in content
    assert "C:\\Users" not in content  # portable — no laptop paths
