"""
CSV Workload Ingestion — Phase 1

Converts CSV rows into validated WorkloadTask objects.

Contract:
- Required columns: id, title
- Optional columns: description, priority, deadline, dependencies, task_type
- Unknown columns are rejected (strict schema).
- Every row problem is collected and reported together in a single
  WorkloadValidationError — invalid tasks are never silently discarded.
- Row order is preserved; errors are reported in row order.

CSV dialect: standard csv module defaults (comma, quote, UTF-8). Dependency
lists inside one cell are semicolon-separated: "T1;T2".
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from app.workload.models import (
    WorkloadTask,
    WorkloadValidationError,
    normalize_dependencies,
    parse_deadline,
)

REQUIRED_COLUMNS = ("id", "title")
OPTIONAL_COLUMNS = ("description", "priority", "deadline", "dependencies", "task_type")
EXPECTED_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
DEFAULT_PRIORITY = 3
DEFAULT_TASK_TYPE = "generic"


def parse_workload_csv(text: str) -> list[WorkloadTask]:
    """Parses CSV text into validated WorkloadTask objects.

    Raises:
        WorkloadValidationError: aggregating every row-level and schema-level
            problem found, in deterministic order.
    """
    if text is None or not text.strip():
        raise WorkloadValidationError(["workload CSV is empty"])

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]

    if not rows:
        raise WorkloadValidationError(["workload CSV has no header row"])

    header = [h.strip() for h in rows[0]]
    errors: list[str] = []

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        errors.append(f"missing required column(s): {', '.join(missing)}")
    unknown = [c for c in header if c not in EXPECTED_COLUMNS]
    if unknown:
        errors.append(f"unknown column(s): {', '.join(unknown)} (allowed: {', '.join(EXPECTED_COLUMNS)})")
    if errors:
        raise WorkloadValidationError(errors)

    col_index = {name: header.index(name) for name in header}

    def cell(row: list[str], name: str) -> str:
        idx = col_index.get(name)
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    seen_ids: dict[str, int] = {}
    tasks: list[WorkloadTask] = []
    row_errors: list[tuple[int, list[str]]] = []

    for line_no, row in enumerate(rows[1:], start=2):
        row_errs: list[str] = []
        if len(row) != len(header):
            row_errs.append(f"expected {len(header)} columns, got {len(row)}")
            row_errors.append((line_no, row_errs))
            continue

        task_id = cell(row, "id")
        title = cell(row, "title")

        if not task_id:
            row_errs.append("missing required field 'id'")
        elif not all(c.isalnum() or c in "_-" for c in task_id) or len(task_id) > 64:
            row_errs.append(f"invalid id '{task_id}': must match ^[a-zA-Z0-9_-]{{1,64}}$")

        if not title:
            row_errs.append("missing required field 'title'")

        priority_raw = cell(row, "priority")
        priority: int | None = None
        if priority_raw == "":
            priority = DEFAULT_PRIORITY
        else:
            try:
                candidate = int(priority_raw)
                if not (1 <= candidate <= 5):
                    raise ValueError
                priority = candidate
            except ValueError:
                row_errs.append(
                    f"invalid priority '{priority_raw}': must be an integer 1-5 (1=highest)"
                )

        deadline_raw = cell(row, "deadline")
        deadline = None
        if deadline_raw != "":
            try:
                deadline = parse_deadline(deadline_raw)
            except ValueError as exc:
                row_errs.append(str(exc))

        deps_raw = cell(row, "dependencies")
        dependencies: list[str]
        try:
            dependencies = normalize_dependencies(deps_raw)
        except ValueError as exc:
            row_errs.append(str(exc))
            dependencies = []

        task_type = cell(row, "task_type") or DEFAULT_TASK_TYPE

        if task_id and task_id in seen_ids:
            row_errs.append(
                f"duplicate task id '{task_id}' (first defined on row {seen_ids[task_id]})"
            )
        elif task_id:
            seen_ids[task_id] = line_no

        if row_errs:
            row_errors.append((line_no, row_errs))
            continue

        try:
            tasks.append(
                WorkloadTask(
                    id=task_id,
                    title=title,
                    description=cell(row, "description"),
                    priority=priority if priority is not None else DEFAULT_PRIORITY,
                    deadline=deadline,
                    dependencies=dependencies or [],
                    task_type=task_type,
                )
            )
        except WorkloadValidationError as exc:
            row_errors.append((line_no, list(exc.errors)))

    if row_errors:
        detailed: list[str] = []
        for line_no, errs in sorted(row_errors):
            for e in errs:
                detailed.append(f"row {line_no}: {e}")
        raise WorkloadValidationError(detailed)

    if not tasks:
        raise WorkloadValidationError(["workload CSV contains no task rows"])

    return tasks


def load_workload_csv(path: str | Path) -> list[WorkloadTask]:
    """Loads and parses a workload CSV file from disk."""
    p = Path(path)
    if not p.is_file():
        raise WorkloadValidationError([f"workload file not found: {p}"])
    return parse_workload_csv(p.read_text(encoding="utf-8-sig"))


__all__ = ["parse_workload_csv", "load_workload_csv", "REQUIRED_COLUMNS", "OPTIONAL_COLUMNS"]
