"""
Orchestration Controller — Phase 2.6

Deterministic controller that coordinates executor → verifier → recovery.

It DOES:
  - select next READY task deterministically
  - call executor, verifier, recovery in order
  - control lifecycle until COMPLETED or FAILED

It DOES NOT:
  - execute tools itself
  - invent tasks or modify plan
  - bypass verification (executor VERIFYING → verifier SUCCEEDED, never direct)
  - use LLM
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.memory.store import ExecutionStore
    from app.tools.registry import ToolRegistry
    from app.orchestration.verifier import VerificationStrategyRegistry
    from app.orchestration.recovery import ExternalStateChecker

from app.orchestration.state import Execution, ExecutionStatus, TaskStatus
from app.orchestration.executor import execute_task
from app.orchestration.verifier import verify_task
from app.orchestration.recovery import recover_task


class OrchestrationError(Exception):
    """Base orchestrator error."""


class ExecutionOrchestrator:
    """
    Deterministic execution controller.

    Dependencies are injected for testability:
      - store: persistence
      - tool_registry: available tools
      - strategy_registry: verification strategies
      - external_state_checker: optional UNKNOWN handler
      - scheduling_policy: optional READY-task ordering hook

    scheduling_policy (Phase 2 extension point):
      Callable[[list[Task]], list[str]] invoked with the currently READY
      tasks each time a task must be selected; returns task_ids in preferred
      execution order. The orchestrator still enforces dependency readiness,
      state transitions, executor/verifier/recovery flow — the policy only
      chooses WHICH ready task runs next. When None (default), selection is
      exactly the legacy behavior: smallest task_id first.
    """

    def __init__(
        self,
        store: ExecutionStore,
        tool_registry: ToolRegistry,
        strategy_registry: VerificationStrategyRegistry,
        external_state_checker: Callable | None = None,
        max_steps: int = 100,
        scheduling_policy: Callable[[list], list[str]] | None = None,
    ) -> None:
        self.store = store
        self.tool_registry = tool_registry
        self.strategy_registry = strategy_registry
        self.external_state_checker = external_state_checker
        self.max_steps = max_steps
        self.scheduling_policy = scheduling_policy

    def run(self, execution_id: str) -> Execution:
        """
        Runs execution to completion deterministically.

        Loop:
          READY → execute_task → VERIFYING → verify_task → SUCCEEDED
                                            ↓ FAILED → recover_task → READY/FAILED/SUCCEEDED

        Returns isolated Execution with final status COMPLETED or FAILED.

        Bounded by max_steps to prevent infinite loops.
        """
        steps = 0
        while steps < self.max_steps:
            steps += 1
            execution = self.store.get_execution(execution_id)

            # Terminal states
            if execution.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
                return execution

            # Ensure dependency-aware readiness is up to date
            execution.update_task_statuses()
            self.store.update_execution(execution)
            execution = self.store.get_execution(execution_id)

            # Collect tasks by status
            ready_tasks = [t for t in execution.tasks.values() if t.status == TaskStatus.READY]
            failed_tasks = [t for t in execution.tasks.values() if t.status == TaskStatus.FAILED]
            verifying_tasks = [t for t in execution.tasks.values() if t.status == TaskStatus.VERIFYING]

            # If there are VERIFYING tasks left (should not happen normally because we verify immediately),
            # verify them first before executing new READY tasks
            if verifying_tasks:
                # Deterministic choice: smallest task_id
                verifying_tasks.sort(key=lambda t: t.task_id)
                task_id = verifying_tasks[0].task_id
                execution = verify_task(execution, task_id, self.store, self.strategy_registry)
                verified_task = execution.tasks[task_id]
                if verified_task.status == TaskStatus.SUCCEEDED:
                    continue
                elif verified_task.status == TaskStatus.FAILED:
                    # Recovery path
                    execution = recover_task(execution, task_id, self.store, self.external_state_checker)
                    recovered = execution.tasks[task_id]
                    if recovered.status == TaskStatus.READY:
                        continue
                    elif recovered.status == TaskStatus.SUCCEEDED:
                        continue
                    else:  # FAILED remain
                        if execution.status == ExecutionStatus.FAILED:
                            return execution
                        # No retry possible, stop
                        return execution
                continue

            # No READY tasks but there are FAILED tasks that haven't been recovered
            # (e.g., executor direct failure path)
            if not ready_tasks and failed_tasks:
                # Recovery for each failed (deterministic order)
                failed_tasks.sort(key=lambda t: t.task_id)
                task_id = failed_tasks[0].task_id
                execution = recover_task(execution, task_id, self.store, self.external_state_checker)
                recovered = execution.tasks[task_id]
                if recovered.status == TaskStatus.READY:
                    continue
                elif recovered.status == TaskStatus.SUCCEEDED:
                    continue
                else:
                    return execution

            if not ready_tasks:
                # No work left — check if all succeeded
                all_succeeded = all(t.status == TaskStatus.SUCCEEDED for t in execution.tasks.values()) if execution.tasks else False
                if all_succeeded and execution.status != ExecutionStatus.COMPLETED:
                    # Verifier/recovery should have marked COMPLETED, but ensure
                    # If execution is still VERIFYING/RECOVERING/EXECUTING, transition if possible
                    # Already handled by verifier/recovery; just return current
                    return execution
                # Check if blocked/pending but not ready -> dependency not met, may be waiting
                # If no ready and not all succeeded and no failed, we're stuck (e.g., circular deps)
                # Return as is
                return execution

            # Execute next READY task (deterministic).
            # A non-None scheduling_policy chooses WHICH ready task runs next
            # (workload priority/deadline); readiness itself remains enforced
            # above — a blocked task can never be selected regardless of rank.
            if self.scheduling_policy is not None:
                ordered_ids = [
                    tid
                    for tid in self.scheduling_policy(ready_tasks)
                    if any(t.task_id == tid for t in ready_tasks)
                ]
                if not ordered_ids:
                    ordered_ids = [t.task_id for t in ready_tasks]
                    ordered_ids.sort()
                task_id = ordered_ids[0]
            else:
                ready_tasks.sort(key=lambda t: t.task_id)
                task_id = ready_tasks[0].task_id

            # Executor
            execution = execute_task(execution, task_id, self.store, self.tool_registry)
            task_after_exec = execution.tasks[task_id]

            if task_after_exec.status == TaskStatus.VERIFYING:
                # Must go through verifier — never bypass
                execution = verify_task(execution, task_id, self.store, self.strategy_registry)
                verified = execution.tasks[task_id]
                if verified.status == TaskStatus.SUCCEEDED:
                    continue
                elif verified.status == TaskStatus.FAILED:
                    execution = recover_task(execution, task_id, self.store, self.external_state_checker)
                    recovered = execution.tasks[task_id]
                    if recovered.status == TaskStatus.READY:
                        continue
                    elif recovered.status == TaskStatus.SUCCEEDED:
                        continue
                    else:
                        return execution
            elif task_after_exec.status == TaskStatus.FAILED:
                # Direct executor failure (no VERIFYING)
                execution = recover_task(execution, task_id, self.store, self.external_state_checker)
                recovered = execution.tasks[task_id]
                if recovered.status == TaskStatus.READY:
                    continue
                elif recovered.status == TaskStatus.SUCCEEDED:
                    continue
                else:
                    return execution
            else:
                # Unexpected, continue loop
                continue

        # Max steps exceeded — return current execution
        return self.store.get_execution(execution_id)


__all__ = ["ExecutionOrchestrator", "OrchestrationError"]
