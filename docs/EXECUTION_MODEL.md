# Execution Model

## Purpose

This document defines the deterministic execution model used by the
Autonomous Project Delivery Agent.

The LLM is responsible for reasoning and proposing actions.

The execution engine is responsible for:

- state
- sequencing
- tool invocation
- validation
- verification
- retries
- recovery
- persistence
- idempotency
- cancellation
- audit history

The execution engine is the source of truth for workflow state.

---

# 1. Execution Lifecycle

Every workflow follows:

REQUEST
  |
  v
PENDING
  |
  v
PLANNING
  |
  v
EXECUTING
  |
  v
VERIFYING
  |
  +---- SUCCESS ----> COMPLETED
  |
  +---- FAILURE ----> RECOVERING
                         |
                         +---- RETRY ----> EXECUTING
                         |
                         +---- FALLBACK -> EXECUTING
                         |
                         +---- REPLAN ---> PLANNING
                         |
                         +---- UNSAFE ---> FAILED

An execution may also transition to:

CANCELLED

Cancellation must be explicit and safe.

---

# 2. Execution Entity

Every execution receives a globally unique:

execution_id

Example:

exec_01JXYZ...

The execution record should contain:

- execution_id
- objective
- status
- created_at
- updated_at
- plan
- current_step_id
- completed_steps
- failed_steps
- retry_count
- recovery_count
- last_error
- verification_results
- checkpoints
- final_result
- audit_events

The execution record must be serializable.

---

# 3. Execution Status

Allowed top-level statuses:

PENDING
PLANNING
EXECUTING
VERIFYING
RECOVERING
COMPLETED
FAILED
CANCELLED

No arbitrary status strings should be introduced without updating the
state transition rules and tests.

---

# 4. Task Model

An execution plan consists of ordered or dependency-aware tasks.

Each task contains:

- task_id
- execution_id
- title
- description
- tool_name
- input
- depends_on
- status
- attempt_count
- max_attempts
- created_at
- started_at
- completed_at
- result
- verification
- error
- recovery_strategy

Task statuses:

PENDING
READY
RUNNING
VERIFYING
SUCCEEDED
FAILED
BLOCKED
CANCELLED

---

# 5. Task Dependencies

Tasks may depend on other tasks.

Example:

TASK A
  |
  v
TASK B
  |
  +----> TASK C
  |
  +----> TASK D

A task is READY only when every required dependency has succeeded.

Do not execute blocked tasks.

Dependency resolution must be deterministic.

---

# 6. Planner Contract

The planner receives:

- user objective
- available context
- available tools
- workflow constraints

The planner returns a structured execution plan.

The planner must not directly execute side effects.

A plan should contain:

- task IDs
- task descriptions
- dependencies
- selected tools
- expected outputs
- verification method
- risk level
- retry policy

The planner must produce machine-readable output.

Do not rely on parsing arbitrary natural-language plans.

---

# 7. Executor Contract

The executor receives a validated task.

Before execution it must:

1. Check task status.
2. Check dependencies.
3. Validate tool input.
4. Check idempotency requirements.
5. Create an execution checkpoint.
6. Mark the task RUNNING.
7. Invoke the tool.
8. Persist the raw tool outcome.
9. Transition to VERIFYING.

The executor does not decide whether the task was successful
based only on the absence of an exception.

Verification is a separate responsibility.

---

# 8. Verification Contract

Every externally meaningful action should have a verification strategy.

Examples:

Action:
create task

Verification:
query task and confirm existence.

Action:
run tests

Verification:
inspect test exit code and test results.

Action:
write artifact

Verification:
check file existence, content and expected structure.

Verification result:

- verified_success
- verified_failure
- unknown

Unknown is not success.

---

# 9. Failure Classification

Failures must be classified before recovery.

Categories:

TRANSIENT
RATE_LIMITED
TIMEOUT
NETWORK
AUTHENTICATION
VALIDATION
CONFLICT
NOT_FOUND
PERMANENT
UNKNOWN

The classifier may use deterministic error mapping first.

The LLM may assist with interpretation of unfamiliar errors,
but application code remains responsible for the final recovery policy.

---

# 10. Retry Policy

Retries must be bounded.

Every retryable task has:

max_attempts

and optionally:

backoff_seconds
max_backoff_seconds

Example policy:

attempt 1 -> immediate retry
attempt 2 -> short backoff
attempt 3 -> longer backoff
attempt limit -> recovery decision

Never retry forever.

Never retry blindly.

---

# 11. Unknown Execution State

This is a critical case.

Example:

1. Tool request is sent.
2. Network connection times out.
3. The client does not know whether the operation completed.

DO NOT immediately retry.

First:

CHECKPOINT
  |
  v
VERIFY EXTERNAL STATE
  |
  +---- operation exists ----> SUCCESS
  |
  +---- operation absent ----> SAFE RETRY
  |
  +---- state still unknown -> RECOVERY / HUMAN REVIEW

Unknown state must be treated as unsafe to duplicate.

---

# 12. Idempotency

Every side-effecting tool must define whether the operation is:

- idempotent
- conditionally idempotent
- non-idempotent

Preferred design:

operation_id

An operation ID should be stable across retries of the same logical task.

Example:

execution_id = exec_123
task_id       = task_004
operation_id  = exec_123:task_004

A retry of task_004 must reuse the same operation ID.

---

# 13. Idempotency Strategy

For supported external systems:

1. Send an idempotency key where supported.
2. Otherwise check whether the intended state already exists.
3. Only perform the mutation if it is safe.
4. Verify final state.

Never use:

random new ID per retry

when the external operation can create duplicates.

---

# 14. Checkpointing

Checkpoint after meaningful execution boundaries.

Minimum checkpoints:

- after plan creation
- before side effect
- after side effect
- after verification
- before recovery
- after recovery

A checkpoint should make it possible to resume without replaying
completed unsafe operations.

---

# 15. Resume Behavior

If the process crashes:

1. Load execution state.
2. Determine the last durable checkpoint.
3. Identify the current task.
4. Determine whether the previous operation is known to have succeeded.
5. Verify unknown state where necessary.
6. Resume from the safest valid point.

Never restart the entire workflow automatically.

---

# 16. Recovery Engine

The recovery engine receives:

- execution state
- failed task
- failure classification
- attempt count
- available tools
- recovery policies

It can select:

RETRY
FALLBACK
REPLAN
PAUSE
FAIL

The selected strategy must be persisted before execution continues.

---

# 17. Recovery Boundaries

Recovery must be bounded by:

- maximum attempts
- maximum recovery cycles
- execution timeout
- tool-specific limits

Example:

max_task_attempts = 3
max_recovery_cycles = 2

If limits are exhausted:

FAILED

The system must produce a useful failure report.

---

# 18. Replanning

Replanning is allowed when:

- a required dependency changes
- a tool becomes unavailable
- the original plan becomes invalid
- a task fails permanently but an alternate path exists

Replanning should preserve completed successful work.

Do not regenerate the entire plan unnecessarily.

Preferred:

existing successful state
+
remaining objective
+
new constraints

-> revised remaining plan

---

# 19. Cancellation

A user may cancel an execution.

Cancellation behavior:

1. Persist cancellation request.
2. Stop scheduling new tasks.
3. Allow currently running safe operations to finish where appropriate.
4. Persist final state.
5. Mark execution CANCELLED.

Destructive operations must not be left in an ambiguous state.

---

# 20. Concurrency

The MVP should prefer controlled sequential execution.

Parallel execution may be added only when:

- tasks are independent
- state conflicts are impossible
- the complexity is justified
- observability remains clear

Do not introduce concurrency merely to make the architecture look
more sophisticated.

Reliability has priority over throughput.

---

# 21. Tool Result Contract

Every tool should return structured information.

Example:

{
  "success": true,
  "status": "completed",
  "operation_id": "...",
  "data": {...},
  "error": null
}

Failure:

{
  "success": false,
  "status": "failed",
  "operation_id": "...",
  "data": null,
  "error": {
    "type": "timeout",
    "message": "..."
  }
}

Tool responses should be machine-readable.

---

# 22. Verification Evidence

Every verified task should produce evidence.

Evidence may include:

- command output
- test results
- resource ID
- database record
- file hash
- file existence
- API response
- structured verification result
- cloud log reference

Evidence should reference:

execution_id
task_id
operation_id

This creates an auditable execution trail.

---

# 23. Audit Events

The system should record events such as:

EXECUTION_CREATED
PLAN_CREATED
TASK_READY
TASK_STARTED
TOOL_CALLED
TOOL_SUCCEEDED
TOOL_FAILED
VERIFICATION_STARTED
VERIFICATION_SUCCEEDED
VERIFICATION_FAILED
RECOVERY_STARTED
RECOVERY_SELECTED
RETRY_STARTED
REPLAN_STARTED
CHECKPOINT_CREATED
EXECUTION_COMPLETED
EXECUTION_FAILED
EXECUTION_CANCELLED

Events should be append-oriented where practical.

---

# 24. Event Ordering

Events should contain:

- event_id
- execution_id
- task_id
- event_type
- timestamp
- actor
- metadata

The actor may be:

USER
AGENT
SYSTEM
TOOL
VERIFIER
RECOVERY_ENGINE

Do not depend solely on wall-clock ordering when correctness depends
on sequence.

Use sequence numbers where useful.

---

# 25. Risk Classification

Tasks should have a risk level:

LOW
MEDIUM
HIGH

LOW:
safe automated actions

MEDIUM:
actions that may require approval depending on context

HIGH:
irreversible or consequential actions

The policy engine determines whether approval is required.

---

# 26. Approval State

Approval-required tasks may use:

WAITING_FOR_APPROVAL

Flow:

PLANNING
  |
  v
WAITING_FOR_APPROVAL
  |
  +---- APPROVED ----> EXECUTING
  |
  +---- REJECTED ----> CANCELLED / REPLAN

Approval decisions must be persisted.

---

# 27. Timeouts

Every external operation should have a timeout.

Timeouts must produce a structured error.

A timeout does NOT automatically mean the operation failed.

If the operation can have completed remotely, the system must verify
external state before retrying.

---

# 28. Execution Time Budget

A workflow should have a maximum execution duration.

Example:

workflow_timeout_seconds

When the deadline is reached:

1. stop starting new tasks
2. persist current state
3. attempt safe cleanup if needed
4. mark execution FAILED or PAUSED
5. report unfinished tasks

---

# 29. Deterministic Failure Injection

The demo environment should support controlled failure injection.

Example modes:

- forced_timeout
- forced_tool_failure
- forced_verification_failure

Failure injection must be clearly separated from production behavior.

It must never silently affect normal execution.

Purpose:

Demonstrate genuine recovery behavior in a repeatable way.

---

# 30. Evaluation Scenarios

Minimum evaluation scenarios:

## Scenario A - Happy Path

Goal completes successfully.

Expected:
COMPLETED

## Scenario B - Transient Failure

Tool fails once, then succeeds.

Expected:
RECOVERING
-> RETRY
-> VERIFIED
-> COMPLETED

## Scenario C - Unknown State

Tool times out after possibly completing.

Expected:
VERIFY
-> detect actual state
-> avoid duplicate
-> COMPLETED

## Scenario D - Permanent Failure

Tool cannot complete task.

Expected:
FAILED

## Scenario E - Resume

Process terminates after checkpoint.

Expected:
load state
-> verify current task
-> resume safely

## Scenario F - Cancellation

User cancels execution.

Expected:
CANCELLED

---

# 31. Testing Requirements

Unit tests should cover:

- state transitions
- task dependency resolution
- retry limits
- failure classification
- idempotency
- checkpoint creation
- resume behavior
- cancellation
- recovery policies

Integration tests should cover:

- ADK agent
- actual tools
- persistence layer
- verification layer

Do not require production credentials for unit tests.

---

# 32. MVP Simplification

The hackathon MVP should initially use:

- sequential execution
- bounded retries
- deterministic failure injection
- local persistent state
- structured tool contracts

Distributed execution and complex scheduling can be introduced only
if the core system is already reliable.

---

# 33. Future Cloud Execution

When migrated to Google Cloud:

Local execution model remains the source of truth.

Cloud components may provide:

- Cloud Run for API/agent runtime
- Firestore for durable state
- Pub/Sub for asynchronous events
- Cloud Logging for observability

The semantics of the execution state machine must remain unchanged
between local and cloud environments.

---

# 34. Core Invariants

The following invariants must always hold:

1. COMPLETED means final verification succeeded.
2. FAILED means the system has stopped execution safely.
3. A task cannot execute before dependencies succeed.
4. A retry must respect the same logical operation identity.
5. Unknown operation state must be verified before replay.
6. Every side effect must have an observable result.
7. Important state must be durable before risky recovery decisions.
8. Retries are bounded.
9. Recovery is bounded.
10. The LLM cannot bypass deterministic execution controls.

---

# 35. Definition of Done

The execution engine is complete when it can:

1. Accept a goal.
2. Generate a structured plan.
3. Execute multiple tasks.
4. Persist state.
5. Verify task results.
6. Detect a controlled failure.
7. Recover without duplicating side effects.
8. Resume after interruption.
9. Produce an auditable event history.
10. Reach a verified final outcome.

The system should demonstrate that it can recover from a broken execution
path without requiring the user to manually reconstruct the workflow.
