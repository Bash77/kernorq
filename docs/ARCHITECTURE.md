# Architecture

## Product

Autonomous Project Delivery Agent.

The system takes a high-level project-delivery objective and autonomously
coordinates a multi-step workflow from discovery through verified completion.

## Core Loop

Goal
-> Understand
-> Plan
-> Execute
-> Observe
-> Verify
-> Persist
-> Recover if needed
-> Complete

## Core Components

### Web Dashboard
Displays objective, plan, execution state, tool activity, failures,
recovery and final evidence.

### Agent API
Provides the interface for starting, inspecting and cancelling executions.

### Google ADK Root Agent
Coordinates reasoning and tool selection.

### Planner
Converts a high-level objective into structured executable steps.

### Executor
Runs approved tools in sequence.

### Verifier
Determines whether an action actually succeeded.

### Recovery Engine
Handles recoverable failures, retries and alternate execution strategies.

### Persistent State
Stores execution state, checkpoints, tool results and recovery history.

### Background Execution
Allows workflows to continue asynchronously without requiring continuous
user interaction.

## Initial Tool Surface

- inspect_repository
- inspect_requirements
- run_project_tests
- create_or_update_task
- generate_submission_artifact
- verify_completion

Additional tools require explicit justification.

## State Machine

PENDING
PLANNING
EXECUTING
VERIFYING
RECOVERING
COMPLETED
FAILED
CANCELLED

## Cloud Architecture

Cloud Run
|
+-- Google ADK Agent
|
+-- Firestore
|     +-- execution state
|     +-- checkpoints
|     +-- results
|
+-- Pub/Sub
|     +-- background events
|
+-- Cloud Logging
      +-- execution evidence

## Technology

Python 3.12
Google ADK
Gemini 3.5 Flash or newer
Google Cloud
Cloud Run
Firestore
Pub/Sub
Cloud Logging

Exact production services should be introduced only when required.

## Architecture Principles

1. LLM decides what should happen.
2. Deterministic application code decides how it happens.
3. External side effects require validation.
4. Important state is persisted.
5. Retries are bounded.
6. Unknown execution state must be verified before retry.
7. Tools are narrow and independently testable.
8. Secrets never enter source control.
9. The simplest architecture that satisfies the requirement is preferred.
10. Every major action should produce observable evidence.

## Competitive Differentiator

The product is not a generic AI assistant.

The core differentiator is:

Autonomous execution + verification + persistent state + failure recovery.

The user provides the goal.

The agent owns the workflow.

## MVP Boundary

The MVP supports one highly polished project-delivery workflow.

It does not attempt to become a general personal operating system.

## Definition of Architectural Success

A workflow must survive a controlled failure, recover safely,
resume execution, and produce verifiable evidence of completion.
