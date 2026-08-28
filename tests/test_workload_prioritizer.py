"""
Workload prioritization + dependency resolution tests — Phase 1.

Covers: priority ordering, deadline ordering, deterministic tie-breaking,
dependency precedence (B never before A), satisfied/unsatisfied/missing/
circular dependencies.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.workload import (
    DependencyCycleError,
    WorkloadTask,
    build_execution_order,
    build_workload_plan,
    prioritize_ready,
)
from app.workload.models import WorkloadStatus


def _t(tid: str, priority: int = 3, deadline: date | None = None, deps: list[str] | None = None) -> WorkloadTask:
    return WorkloadTask(id=tid, title=f"Task {tid}", priority=priority, deadline=deadline, dependencies=deps or [])


# ---------------------------------------------------------------------------
# Prioritization
# ---------------------------------------------------------------------------

def test_priority_ordering_higher_priority_first():
    ordered = prioritize_ready([_t("LOW", 5), _t("HIGH", 1), _t("MID", 3)])
    assert [t.id for t in ordered] == ["HIGH", "MID", "LOW"]


def test_deadline_ordering_earlier_first():
    ordered = prioritize_ready(
        [
            _t("LATE", 2, date(2026, 12, 1)),
            _t("EARLY", 2, date(2026, 9, 1)),
            _t("MIDDAY", 2, date(2026, 10, 15)),
        ]
    )
    assert [t.id for t in ordered] == ["EARLY", "MIDDAY", "LATE"]


def test_no_deadline_sorts_last_within_same_priority():
    ordered = prioritize_ready(
        [_t("NO_DATE", 2), _t("DATED", 2, date(2027, 1, 1))]
    )
    assert [t.id for t in ordered] == ["DATED", "NO_DATE"]


def test_deterministic_tie_breaking_by_task_id():
    tasks = [_t("ZULU", 3), _t("ALPHA", 3), _t("MIKE", 3)]
    a = [t.id for t in prioritize_ready(tasks)]
    b = [t.id for t in prioritize_ready(list(reversed(tasks)))]
    assert a == b == ["ALPHA", "MIKE", "ZULU"]


def test_priority_beats_deadline_and_id():
    tasks = [
        _t("A_HIGH_LATE", 1, date(2026, 12, 31)),
        _t("B_LOW_SOON", 5, date(2026, 1, 1)),
    ]
    assert [t.id for t in prioritize_ready(tasks)] == ["A_HIGH_LATE", "B_LOW_SOON"]


# ---------------------------------------------------------------------------
# Dependency precedence in the global order
# ---------------------------------------------------------------------------

def test_dependent_never_before_dependency():
    # Spec example: A prio 2, B prio 5 depends on A, C prio 1
    tasks = [
        _t("A", 2),
        _t("B", 5, deps=["A"]),
        _t("C", 1),
    ]
    order = build_execution_order(tasks, valid_ids={t.id for t in tasks})
    assert order.index("A") < order.index("B")
    # C is highest priority with no deps -> first overall
    assert order[0] == "C"
    assert order == ["C", "A", "B"]


def test_high_priority_dependent_waits_for_low_priority_dependency():
    tasks = [
        _t("DEP", 5),                 # low priority dependency
        _t("URGENT_CHILD", 1, deps=["DEP"]),
    ]
    order = build_execution_order(tasks, {t.id for t in tasks})
    assert order == ["DEP", "URGENT_CHILD"]  # readiness dominates priority


def test_execution_order_is_total_and_deterministic():
    tasks = [
        _t("T5", 2, date(2026, 9, 3)),
        _t("T1", 1),
        _t("T4", 2, date(2026, 9, 2), deps=["T1"]),
        _t("T3", 2, date(2026, 9, 1)),
        _t("T2", 3, deps=["T3", "T4"]),
    ]
    ids = {t.id for t in tasks}
    o1 = build_execution_order(tasks, ids)
    o2 = build_execution_order(list(reversed(tasks)), ids)
    assert o1 == o2
    assert sorted(o1) == sorted(ids)
    assert o1.index("T1") < o1.index("T4")
    assert all(o1.index(d) < o1.index("T2") for d in ("T3", "T4"))


def test_build_execution_order_safety_net_on_cycle():
    from app.workload.models import DependencyCycleError as DCE

    tasks = [_t("X", deps=["Y"]), _t("Y", deps=["X"])]
    with pytest.raises(RuntimeError):
        build_execution_order(tasks, {"X", "Y"})
    # And the planner itself raises the clear typed error instead:
    with pytest.raises(DependencyCycleError):
        build_workload_plan(tasks)


# ---------------------------------------------------------------------------
# Dependency resolution via the planner
# ---------------------------------------------------------------------------

def test_task_with_no_dependencies_is_ready():
    plan = build_workload_plan([_t("SOLO")])
    assert plan.status_of("SOLO") == WorkloadStatus.READY
    assert plan.ready_task_ids == ["SOLO"]
    assert plan.blocked_task_ids == []
    assert plan.invalid_task_ids == []


def test_satisfied_structure_child_blocked_until_parent_runs():
    """At planning time 'satisfied' means: parent exists and will run first."""
    plan = build_workload_plan([_t("P"), _t("CHILD", deps=["P"])])
    assert plan.status_of("P") == WorkloadStatus.READY
    assert plan.status_of("CHILD") == WorkloadStatus.BLOCKED
    assert plan.execution_order == ["P", "CHILD"]


def test_unsatisfied_dependency_blocks_task():
    plan = build_workload_plan([_t("ROOT"), _t("MID", deps=["ROOT"]), _t("LEAF", deps=["MID"])])
    assert plan.status_of("ROOT") == WorkloadStatus.READY
    assert plan.status_of("MID") == WorkloadStatus.BLOCKED
    assert plan.status_of("LEAF") == WorkloadStatus.BLOCKED
    assert "waiting on: MID" in plan.entries["LEAF"].reason


def test_missing_dependency_id_marks_task_invalid_not_crash():
    plan = build_workload_plan([_t("ORPHAN", deps=["GHOST"]), _t("FINE")])
    assert plan.status_of("ORPHAN") == WorkloadStatus.INVALID
    assert "GHOST" in plan.entries["ORPHAN"].reason
    assert plan.status_of("FINE") == WorkloadStatus.READY
    # Invalid task excluded from execution order entirely
    assert plan.execution_order == ["FINE"]
    assert plan.invalid_task_ids == ["ORPHAN"]


def test_dependent_of_invalid_task_also_unschedulable():
    plan = build_workload_plan(
        [_t("GHOSTLESS", deps=["MISSING"]), _t("CHILD", deps=["GHOSTLESS"]), _t("OK")]
    )
    assert plan.status_of("GHOSTLESS") == WorkloadStatus.INVALID
    assert plan.status_of("CHILD") == WorkloadStatus.INVALID
    assert "unschedulable" in plan.entries["CHILD"].reason
    assert plan.execution_order == ["OK"]


def test_circular_dependency_produces_clear_error():
    with pytest.raises(DependencyCycleError) as excinfo:
        build_workload_plan([_t("A", deps=["C"]), _t("B", deps=["A"]), _t("C", deps=["B"])])
    assert set(excinfo.value.cycle) == {"A", "B", "C"}
    assert "->" in str(excinfo.value)  # readable chain in message


def test_self_cycle_rejected_by_model_validation():
    with pytest.raises(Exception):
        WorkloadTask(id="S", title="S", dependencies=["S"])
