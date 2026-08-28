"""
Workload-aware scheduling tests — Phase 2.

Proves the chain: planner order -> scheduler decision -> ACTUAL execution
order, using a call-recording tool registry. Also proves backwards
compatibility (no policy -> legacy task_id scheduling) and that existing
failure/recovery behavior stays intact under the workload policy.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.memory.store import InMemoryExecutionStore
from app.orchestration.orchestrator import ExecutionOrchestrator
from app.orchestration.planner import create_execution_from_plan
from app.orchestration.state import ExecutionStatus, TaskStatus
from app.tools.registry import ToolRegistry
from app.workload import (
    WorkloadSchedulingPolicy,
    WorkloadTask,
    parse_workload_csv,
    run_workload,
)


class RecordingRegistry:
    """Builds a ToolRegistry whose tools append to a shared invocation log."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.registry = ToolRegistry()

    def add_ok(self, name: str) -> None:
        def _tool(**_kw):
            self.calls.append(name)
            return {"success": True, "files": [name], "data": {"marker": name}}

        self.registry.register(name, _tool)

    def add_failing_permanent(self, name: str) -> None:
        def _tool(**_kw):
            self.calls.append(name)
            return {
                "success": False,
                "error": {"type": "InvalidPathError", "message": f"{name} deterministic failure"},
            }

        self.registry.register(name, _tool)

    def add_flaky(self, name: str, fail_first_n: int = 1) -> None:
        state = {"n": 0}

        def _tool(**_kw):
            self.calls.append(name)
            state["n"] += 1
            if state["n"] <= fail_first_n:
                raise RuntimeError(f"{name} transient failure {state['n']}")
            return {"success": True, "files": [name], "data": {"marker": name}}

        self.registry.register(name, _tool)


def _t(tid: str, priority: int = 3, deadline: date | None = None, deps: list[str] | None = None) -> WorkloadTask:
    # task_type defaults to the task id; _run_tasks maps type->same-named tool
    return WorkloadTask(id=tid, title=f"Task {tid}", priority=priority, deadline=deadline,
                        dependencies=deps or [], task_type=tid)


def _run_tasks(tasks: list[WorkloadTask], rec: RecordingRegistry, max_attempts: int = 3):
    from app.orchestration.verifier import create_default_strategy_registry

    return run_workload(
        tasks,
        objective="workload scheduling test",
        store=InMemoryExecutionStore(),
        tool_registry=rec.registry,
        strategy_registry=create_default_strategy_registry(),
        tool_mapping={t.task_type: t.task_type for t in tasks},
        default_tool_input={"directory_path": "."},
        max_attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# Priority drives ACTUAL execution order
# ---------------------------------------------------------------------------

def test_priority_order_actually_executes_b_c_a():
    """Spec case: A=3, B=1, C=2 with no deps -> actual invocation order B, C, A."""
    rec = RecordingRegistry()
    for name in ("task_A", "task_B", "task_C"):
        rec.add_ok(name)
    tasks = [_t("task_A", 3), _t("task_B", 1), _t("task_C", 2)]

    result = _run_tasks(tasks, rec)

    assert result.execution.status == ExecutionStatus.COMPLETED
    # The proof is in the executor's actual invocation log — not planning output
    assert rec.calls == ["task_B", "task_C", "task_A"]


def test_deadline_orders_actual_execution_when_priority_equal():
    rec = RecordingRegistry()
    for name in ("task_A", "task_B"):
        rec.add_ok(name)
    tasks = [
        _t("task_A", 2, date(2026, 12, 1)),
        _t("task_B", 2, date(2026, 9, 1)),
    ]
    _run_tasks(tasks, rec)
    assert rec.calls == ["task_B", "task_A"]  # earlier deadline first


def test_no_deadline_runs_last_among_equals():
    rec = RecordingRegistry()
    for name in ("task_A", "task_B"):
        rec.add_ok(name)
    tasks = [
        _t("task_A", 2, None),               # no deadline -> last
        _t("task_B", 2, date(2026, 9, 1)),   # dated -> first
    ]
    _run_tasks(tasks, rec)
    assert rec.calls == ["task_B", "task_A"]


def test_deterministic_tie_break_same_priority_same_deadline():
    rec = RecordingRegistry()
    for name in ("z_task", "a_task", "m_task"):
        rec.add_ok(name)
    tasks = [_t("z_task", 3), _t("a_task", 3), _t("m_task", 3)]
    _run_tasks(tasks, rec)
    assert rec.calls == ["a_task", "m_task", "z_task"]


# ---------------------------------------------------------------------------
# Dependency precedence under the policy
# ---------------------------------------------------------------------------

def test_dependency_precedence_c_a_b():
    """Spec case: A=5; B=1 depends on A; C=2 -> actual order C, A, B.

    B has the highest priority but starts BLOCKED — it must never be selected
    merely because it outranks everything else.
    """
    rec = RecordingRegistry()
    for name in ("task_A", "task_B", "task_C"):
        rec.add_ok(name)
    tasks = [
        _t("task_A", 5),
        _t("task_B", 1, deps=["task_A"]),
        _t("task_C", 2),
    ]

    result = _run_tasks(tasks, rec)

    assert result.execution.status == ExecutionStatus.COMPLETED
    assert rec.calls == ["task_C", "task_A", "task_B"]


def test_dynamic_readiness_after_completion():
    """After A succeeds, B becomes READY and is then ranked by the policy."""
    rec = RecordingRegistry()
    for name in ("A", "B", "C"):
        rec.add_ok(name)
    tasks = [
        _t("A", 5),
        _t("B", 1, deps=["A"]),
        _t("C", 4),
    ]
    result = _run_tasks(tasks, rec)
    # Wave dynamics: initial READY {A,C} -> C(4) then A(5); only after A
    # completes does B become READY and run.
    assert rec.calls == ["C", "A", "B"]
    assert result.execution.tasks["B"].status == TaskStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Failure + recovery behavior under the policy
# ---------------------------------------------------------------------------

def test_failed_dependency_never_releases_dependent():
    """Permanent dependency failure must not mark dependents executable."""
    rec = RecordingRegistry()
    rec.add_ok("healthy_child")
    rec.add_ok("bystander")
    rec.add_failing_permanent("broken_dep")

    def _tt(tid: str, priority: int, deps: list[str] | None = None) -> WorkloadTask:
        return WorkloadTask(id=tid, title=tid, priority=priority, dependencies=deps or [], task_type=tid)

    tasks = [
        _tt("broken_dep", 1),
        _tt("healthy_child", 1, deps=["broken_dep"]),
        _tt("bystander", 2),
    ]

    result = _run_tasks(tasks, rec, max_attempts=2)

    assert result.execution.status == ExecutionStatus.FAILED
    assert result.execution.tasks["broken_dep"].status == TaskStatus.FAILED
    assert result.execution.tasks["healthy_child"].status == TaskStatus.BLOCKED
    # Dependent never executed despite top priority ranking
    assert "healthy_child" not in rec.calls
    # Existing engine semantics (preserved, not changed by Phase 2): a
    # permanent failure halts the whole execution — independent bystander
    # tasks do not continue. Documented as an architectural note.
    assert rec.calls == ["broken_dep"]


def test_recovery_remains_intact_and_scheduling_resumes():
    """Transient failure -> existing recovery retries -> dependents proceed."""
    rec = RecordingRegistry()
    rec.add_flaky("flaky_root", fail_first_n=1)
    rec.add_ok("dependent")
    tasks = [
        _t("flaky_root", 1),
        _t("dependent", 1, deps=["flaky_root"]),
    ]

    result = _run_tasks(tasks, rec, max_attempts=3)

    assert result.execution.status == ExecutionStatus.COMPLETED
    # flaky_root invoked twice (attempt 1 failed via recovery, attempt 2 ok)
    assert rec.calls.count("flaky_root") == 2
    assert rec.calls[-1] == "dependent"
    assert result.execution.tasks["flaky_root"].attempt_count == 2
    recovery_entries = result.execution.recovery_history
    assert any(e["result"] == "RETRY_SCHEDULED" for e in recovery_entries)


# ---------------------------------------------------------------------------
# Backwards compatibility + wiring guarantees
# ---------------------------------------------------------------------------

def test_no_policy_preserves_legacy_task_id_scheduling():
    """Orchestrator without a scheduling_policy behaves exactly as before."""
    rec = RecordingRegistry()
    for name in ("zz", "aa", "mm"):
        rec.add_ok(name)

    from app.orchestration.verifier import create_default_strategy_registry

    plan_dict = {
        "objective": "legacy",
        "tasks": [{"task_id": n, "title": n, "description": n, "tool_name": n} for n in ("zz", "aa", "mm")],
    }
    registry = rec.registry
    execution = create_execution_from_plan(plan_dict, registry)
    store = InMemoryExecutionStore()
    store.create_execution(execution)
    orchestrator = ExecutionOrchestrator(store, registry, create_default_strategy_registry())
    result = orchestrator.run(execution.execution_id)

    assert result.status == ExecutionStatus.COMPLETED
    assert rec.calls == ["aa", "mm", "zz"]  # plain task_id ordering


def test_policy_never_selects_blocked_task_even_if_top_ranked():
    """Direct policy-level guard: blocked ids cannot leak through ranking."""
    policy = WorkloadSchedulingPolicy([_t("TOP", 1), _t("LOW", 5)])
    # Only LOW is ready; TOP must not appear just because it ranks higher.
    class FakeRuntimeTask:
        def __init__(self, tid: str) -> None:
            self.task_id = tid

    ordered = policy([FakeRuntimeTask("LOW")])
    assert ordered == ["LOW"]


def test_full_csv_pipeline_executes_in_planned_order():
    """E2E: CSV -> loader -> planner -> manager -> existing engine, mixed
    priorities + deadlines + dependencies, asserting the ACTUAL sequence."""
    csv_text = (
        "id,title,priority,deadline,dependencies,task_type\n"
        "REPORT,Send report,3,,,\n"
        "URGENT_FIX,Hotfix prod,1,,,\n"
        "DOCS,Update docs,4,,,\n"
        "AUDIT,Security audit,2,,,\n"
        "FOLLOWUP,Follow-up review,1,2026-09-01,AUDIT,\n"
    )
    tasks = parse_workload_csv(csv_text)
    # Route each task to its same-named recording tool
    for t in tasks:
        t.task_type = t.id
    rec = RecordingRegistry()
    for name in ("REPORT", "URGENT_FIX", "DOCS", "AUDIT", "FOLLOWUP"):
        rec.add_ok(name)

    from app.orchestration.verifier import create_default_strategy_registry

    result = run_workload(
        tasks,
        objective="Execute weekly workload",
        store=InMemoryExecutionStore(),
        tool_registry=rec.registry,
        strategy_registry=create_default_strategy_registry(),
        tool_mapping={t.task_type: t.task_type for t in tasks},
        default_tool_input={"directory_path": "."},
    )

    assert result.execution.status == ExecutionStatus.COMPLETED
    # Dynamic greedy ranking per decision wave:
    #   wave1 READY {URGENT_FIX(1), AUDIT(2), REPORT(3), DOCS(4)}
    #     -> URGENT_FIX, AUDIT
    #   AUDIT done -> FOLLOWUP(1) joins READY, outranks REPORT(3)/DOCS(4)
    #     -> FOLLOWUP, then REPORT, DOCS
    planned = ["URGENT_FIX", "AUDIT", "FOLLOWUP", "REPORT", "DOCS"]
    assert result.planned_order == planned
    assert rec.calls == planned            # THE assertion: runtime honored it
