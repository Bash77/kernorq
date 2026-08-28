"""
Workload CSV parsing tests — Phase 1.

Covers: valid CSV, missing required fields, invalid priority, invalid
deadline, malformed dependencies, duplicate task IDs, schema errors,
deterministic rejection (no silent discards).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.workload import WorkloadValidationError, parse_workload_csv

VALID_CSV = """id,title,description,priority,deadline,dependencies,task_type
T1,Fix login bug,Auth flow broken on Safari,1,2026-09-01,,bug
T2,Write release notes,,,2026-08-30,T1;T3,docs
T3,Update deps,Quarterly bump,,,,chore
"""


def test_valid_csv_parses_all_fields():
    tasks = parse_workload_csv(VALID_CSV)
    assert [t.id for t in tasks] == ["T1", "T2", "T3"]

    t1 = tasks[0]
    assert t1.title == "Fix login bug"
    assert t1.description == "Auth flow broken on Safari"
    assert t1.priority == 1
    assert t1.deadline == date(2026, 9, 1)
    assert t1.dependencies == []
    assert t1.task_type == "bug"
    assert t1.status.value == "PENDING"

    t2 = tasks[1]
    assert t2.priority == 3  # default when empty
    assert t2.dependencies == ["T1", "T3"]
    assert t2.task_type == "docs"

    t3 = tasks[2]
    assert t3.deadline is None  # empty optional
    assert t3.task_type == "chore"


def test_empty_optionals_get_deterministic_defaults():
    csv_text = "id,title\nX1,Only required fields\n"
    tasks = parse_workload_csv(csv_text)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.description == ""
    assert t.priority == 3
    assert t.deadline is None
    assert t.dependencies == []
    assert t.task_type == "generic"


def test_missing_required_field_rejected_with_row_number():
    csv_text = "id,title\nOK,Fine\n,Bad row\n"
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    msg = str(excinfo.value)
    assert "row 3" in msg
    assert "missing required field 'id'" in msg


def test_missing_title_rejected():
    csv_text = "id,title\nT9,   \n"
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    assert "missing required field 'title'" in str(excinfo.value)


def test_invalid_priority_rejected():
    csv_text = (
        "id,title,priority\n"
        "T1,Ok task,3\n"
        "T2,Bad high,6\n"
        "T3,Bad low,0\n"
        "T4,Not a number,urgent\n"
    )
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    msg = str(excinfo.value)
    assert "row 3" in msg and "'6'" in msg
    assert "row 4" in msg and "'0'" in msg
    assert "row 5" in msg and "'urgent'" in msg


def test_invalid_deadline_format_and_calendar_date_rejected():
    csv_text = (
        "id,title,deadline\n"
        "T1,Slash date,01/09/2026\n"
        "T2,Bad calendar,2026-02-30\n"
        "T3,Good,2026-12-31\n"
    )
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    msg = str(excinfo.value)
    assert "row 2" in msg and "YYYY-MM-DD" in msg
    assert "row 3" in msg and "valid calendar date" in msg
    # Contract: the whole file is rejected — no partial results are returned
    with pytest.raises(WorkloadValidationError):
        parse_workload_csv(csv_text)


def test_malformed_dependencies_rejected():
    csv_text = (
        "id,title,dependencies\n"
        "T1,Empty between,A;;B\n"
        "T2,Trailing sep,A;\n"
        "T3,Leading sep,;A\n"
    )
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    msg = str(excinfo.value)
    assert "empty entry" in msg
    assert "row 2" in msg and "row 3" in msg and "row 4" in msg


def test_dependencies_normalized_whitespace():
    csv_text = 'id,title,dependencies\nT1,T,"A; B ;C"\n'
    tasks = parse_workload_csv(csv_text)
    assert tasks[0].dependencies == ["A", "B", "C"]


def test_duplicate_task_ids_rejected_with_first_row_reference():
    csv_text = (
        "id,title\n"
        "DUP,First\n"
        "OTHER,Middle\n"
        "DUP,Second\n"
    )
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    msg = str(excinfo.value)
    assert "duplicate task id 'DUP'" in msg
    assert "first defined on row 2" in msg


def test_unknown_column_rejected():
    csv_text = "id,title,sneaky\nT1,T,x\n"
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    assert "unknown column(s): sneaky" in str(excinfo.value)


def test_missing_required_column_rejected():
    csv_text = "id,summary\nT1,S\n"
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    assert "missing required column(s): title" in str(excinfo.value)


def test_wrong_column_count_row_rejected():
    csv_text = "id,title\nT1,One,TWO-MANY\n"
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    assert "expected 2 columns, got 3" in str(excinfo.value)


def test_invalid_id_characters_rejected():
    csv_text = "id,title\nbad id!,Spaces not allowed\n"
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    assert "invalid id 'bad id!'" in str(excinfo.value)


def test_self_dependency_rejected_at_model_level():
    csv_text = "id,title,dependencies\nSELF,Self dep,SELF\n"
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    assert "cannot depend on itself" in str(excinfo.value)


def test_empty_and_header_only_inputs_rejected():
    with pytest.raises(WorkloadValidationError):
        parse_workload_csv("")
    with pytest.raises(WorkloadValidationError):
        parse_workload_csv("id,title\n")


def test_all_errors_aggregated_not_first_only():
    csv_text = (
        "id,title,priority,deadline\n"
        ",NoId,bad,also-bad\n"
    )
    with pytest.raises(WorkloadValidationError) as excinfo:
        parse_workload_csv(csv_text)
    errors = excinfo.value.errors
    assert len(errors) >= 3  # missing id + bad priority + bad deadline together


def test_parse_is_deterministic():
    a = parse_workload_csv(VALID_CSV)
    b = parse_workload_csv(VALID_CSV)
    assert [(t.id, t.title, t.priority, t.dependencies) for t in a] == [
        (t.id, t.title, t.priority, t.dependencies) for t in b
    ]
