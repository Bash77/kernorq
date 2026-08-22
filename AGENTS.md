# AGENTS.md - Autonomous Execution Agent

## 1. Project Identity

Project codename: Autonomous Execution Agent

The final public product name has NOT been selected yet.

Do not use JARVIS or ARVIS as the final product name.

Use "agent" or "execution_agent" internally until branding is finalized.

Project type:
- Autonomous AI agent
- Hackathon submission
- Google ADK / Gemini / Google Cloud

Primary objective:

Build a narrow, production-minded autonomous agent that can:

1. Understand a user objective.
2. Convert the objective into an executable plan.
3. Execute actions through tools.
4. Observe and verify results.
5. Persist execution state.
6. Detect failures.
7. Recover safely when possible.
8. Continue from the last known safe state.
9. Produce an auditable execution history.

This is not a chatbot.

The product must demonstrate real autonomous execution.

---

## 2. Competition Strategy

The project is being developed for the All Things Agentic Hackathon.

Priorities:

1. Real autonomous execution
2. Real user value
3. Reliability
4. Failure recovery
5. Persistent state
6. Tool orchestration
7. Security
8. Production-minded engineering
9. Clear demonstration
10. Narrow scope

Do not optimize for feature count.

A smaller reliable system is better than a large fragile system.

---

## 3. Core Execution Loop

The conceptual workflow is:

USER OBJECTIVE
    |
    v
UNDERSTAND
    |
    v
PLAN
    |
    v
EXECUTE
    |
    v
VERIFY
    |
    v
PERSIST
    |
    +---- SUCCESS ----> COMPLETE
    |
    +---- FAILURE ----> RECOVER
                           |
                           v
                     RETRY / PLAN B
                           |
                           v
                         VERIFY

Every autonomous workflow should follow this model.

---

## 4. Architecture Principles

Keep the system modular.

Initial structure:

app/
  agent/
    agent.py

  orchestration/
    planner.py
    executor.py
    recovery.py
    state.py

  tools/
    registry.py

  memory/
    store.py

  api/

  main.py

The exact structure may evolve.

Do not create abstractions before they are justified.

---

## 5. LLM Responsibilities

The LLM is responsible for:

- interpreting objectives
- reasoning about plans
- selecting tools
- determining execution order
- adapting when execution fails
- producing structured decisions

The LLM is NOT the source of truth for application state.

State must be controlled by deterministic application code.

The model must not directly mutate critical state without validation.

---

## 6. Deterministic Execution

Prefer:

LLM decides WHAT should happen.

Application code decides HOW it happens.

Example:

LLM:
"Create task X"

Application:
1. Validate input.
2. Check whether task already exists.
3. Execute the operation.
4. Verify the result.
5. Persist the result.

Do not allow arbitrary uncontrolled external API mutation by the model.

---

## 7. Tool Design

Every tool should have:

- clear name
- explicit input schema
- explicit output schema
- validation
- predictable errors
- useful logging
- idempotency strategy where required

Prefer small tools such as:

create_task()
update_task()
fetch_data()
verify_result()

Avoid giant tools such as:

do_everything()

Each tool should perform one meaningful operation.

---

## 8. Idempotency

Idempotency is a first-class requirement.

The system must avoid performing the same irreversible action twice when a previous attempt may already have succeeded.

Examples:

- duplicate task creation
- duplicate notification
- duplicate upload
- duplicate external mutation
- duplicate database write

Possible mechanisms:

- idempotency keys
- operation IDs
- execution IDs
- state checkpoints
- existence checks
- transaction boundaries

Never blindly retry an operation whose result is unknown.

---

## 9. Failure Recovery

The system must distinguish:

1. Transient failure
2. Permanent failure
3. Validation failure
4. Authentication failure
5. Tool failure
6. Network failure
7. Timeout
8. Unknown execution state

Recovery rules:

Transient failure:
- bounded retry

Rate limit:
- exponential backoff

Timeout:
- determine whether the operation may have succeeded

Known tool failure:
- use fallback strategy if available

Permanent failure:
- stop safely and report

Unknown state:
- verify before retrying

Never implement infinite retries.

All retries must be bounded.

---

## 10. State Management

Important execution state must be explicit and persistent.

At minimum:

- execution_id
- objective
- plan
- current_step
- status
- attempt_count
- tool_result
- error
- timestamps
- recovery information

Suggested statuses:

PENDING
PLANNING
EXECUTING
VERIFYING
RECOVERING
COMPLETED
FAILED
CANCELLED

State transitions must be deterministic and auditable.

---

## 11. Memory

Memory must not become a generic "second brain".

Store information only when it improves execution.

Useful memory:

- successful strategies
- previous failures
- user-approved preferences
- execution history
- tool outcomes
- recovery strategies

Avoid unnecessary personal data.

Keep memory boundaries explicit.

---

## 12. Security

Never hard-code:

- API keys
- passwords
- access tokens
- service credentials
- private keys

Use environment variables for local development.

Use Google Secret Manager for cloud deployment when appropriate.

Never commit .env.

Never log secrets.

Never expose unnecessary credentials to the model.

---

## 13. Human Approval

Autonomy does not mean unrestricted destructive behavior.

Potentially destructive or irreversible actions should support approval.

Examples:

- deleting important data
- sending sensitive communications
- financial transactions
- changing important external resources
- publishing externally

Preferred flow:

PLAN
  |
  v
APPROVAL
  |
  v
EXECUTION

Safe operations can remain fully autonomous.

---

## 14. Google Technology

Google technologies are core to this project.

Prefer:

- Google ADK
- Gemini
- Google Cloud

Do not introduce another agent framework without a concrete technical reason.

Do not add LangChain, CrewAI, AutoGen, or another framework merely because it is popular.

Keep the architecture understandable.

---

## 15. Model Policy

Development models and production models are separate.

Antigravity may use:

- Gemini
- Claude
- GPT-based models
- other available coding models

for development, review, debugging, and planning.

The application itself must use a Google/Gemini model and Google agent stack that satisfies the hackathon requirements.

Do not hard-code the model name throughout the codebase.

Use configuration.

Example:

MODEL_NAME

Do not silently change the production model.

---

## 16. Dependency Policy

Current dependencies:

- google-adk
- python-dotenv

Development dependencies:

- pytest

Do not add packages automatically.

Before adding a dependency:

1. Explain why it is needed.
2. Check whether the standard library can solve the problem.
3. Check whether an existing dependency already solves it.
4. Use the smallest appropriate dependency.

Avoid dependency bloat.

---

## 17. Python Standards

Target Python:

3.12+

Use:

- type hints
- small functions
- clear boundaries
- explicit error handling
- deterministic logic
- structured data where useful

Avoid:

- global mutable state
- hidden side effects
- giant functions
- duplicate business logic
- unnecessary metaprogramming

---

## 18. Testing

Critical behavior must have tests.

Priorities:

1. Planner behavior
2. Tool validation
3. Executor behavior
4. State transitions
5. Retry behavior
6. Recovery behavior
7. Idempotency
8. Failure handling

Unit tests should be deterministic.

Use mocks or fakes rather than live external services for unit tests.

Integration tests may use real services when appropriate.

A feature is not complete until its critical behavior is tested.

---

## 19. Observability

Execution should be understandable from logs.

Useful fields include:

- execution ID
- current step
- selected tool
- tool result
- failure
- retry
- recovery decision
- final result

Example:

[EXECUTION] id=abc123 step=2 tool=create_task
[VERIFY] task_created=true
[ERROR] timeout
[RECOVERY] checking previous operation
[RECOVERY] operation_not_completed=true
[RETRY] attempt=2
[COMPLETE] id=abc123

Never log secrets or unnecessary sensitive information.

---

## 20. API Strategy

Keep the API small.

Preferred operations:

POST /executions
GET /executions/{id}
POST /executions/{id}/cancel

Do not build a large API before the product requires it.

---

## 21. UI Strategy

The UI exists to demonstrate autonomous execution.

Prioritize:

- objective
- plan
- current step
- tool execution
- success/failure
- recovery
- final result
- execution history

Do not spend most of the hackathon building decorative UI.

The execution engine is the priority.

---

## 22. Demo Requirements

The final demo should prove:

1. User provides a real objective.
2. Agent creates a plan.
3. Agent performs multiple steps.
4. Real state changes occur.
5. Agent verifies results.
6. A failure occurs.
7. Agent detects the failure.
8. Agent recovers.
9. Agent completes the workflow.
10. Execution history proves what happened.
11. Google Cloud deployment is visible in the final submission.

Do not fake execution.

Do not fabricate logs.

Do not simulate success when the real system can demonstrate it.

---

## 23. Failure Demo

The project should include a deterministic failure scenario.

Example:

Step 2 fails intentionally.

Expected behavior:

1. Detect failure.
2. Classify failure.
3. Inspect state.
4. Determine whether retry is safe.
5. Execute recovery.
6. Verify result.
7. Continue execution.

The failure path must be real.

---

## 24. Scope Control

These are NOT automatically part of the MVP:

- calendar integration
- email integration
- WhatsApp
- Slack
- Discord
- social media automation
- smart home control
- financial transactions
- full personal knowledge graph
- mobile application
- desktop application
- voice assistant
- multi-agent swarm
- custom vector database
- RAG system
- autonomous browser
- dozens of integrations

Add them only if they materially improve:

- judging score
- user value
- technical credibility
- demo impact

---

## 25. Product Principle

Do not build:

"An AI that can do everything."

Build:

"An agent that can reliably complete a difficult class of tasks without constant human supervision."

Priority order:

Reliability > breadth

Execution > conversation

Verification > assumption

Recovery > blind retry

Proof > claims

---

## 26. Anti-Patterns

Never:

- add technology because it is trendy
- create unnecessary microservices
- create unnecessary agents
- create unnecessary databases
- create unnecessary abstractions
- install packages without justification
- build features without tests
- expose secrets
- trust model-generated state blindly
- use infinite retries
- fake demo results
- optimize UI before execution works
- expand scope because another project has a feature

---

## 27. Change Control

Before making a major architectural change, ask:

1. Does it improve autonomous execution?
2. Does it improve reliability?
3. Does it improve demonstrability?
4. Does it support the judging criteria?
5. Can it be implemented within the remaining hackathon time?
6. Does it introduce unnecessary complexity?

If most answers are no, do not implement it.

---

## 28. Git Discipline

Use small meaningful commits.

Examples:

feat: add execution state model
feat: add tool registry
feat: add recovery engine
test: add idempotency tests
fix: handle timeout recovery
docs: document architecture

Never commit:

- .env
- credentials
- private keys
- unnecessary binaries
- large generated files

---

## 29. Definition of Done

A feature is DONE only when:

- implementation exists
- inputs are validated
- errors are handled
- important state is persisted
- critical behavior is tested
- logs are understandable
- secrets are protected
- documentation is updated when necessary
- existing tests pass

"Works once on my machine" is not DONE.

---

## 30. Agent Operating Rules

When modifying this repository:

1. Inspect existing code first.
2. Do not overwrite working code unnecessarily.
3. Do not create duplicate modules.
4. Do not install dependencies without justification.
5. Run relevant tests after meaningful changes.
6. Keep changes focused.
7. Explain important architectural tradeoffs.
8. Preserve working behavior.
9. Never expose secrets.
10. Never invent external API behavior.
11. Never claim something works without testing it.
12. Prefer the simplest implementation that satisfies the requirement.

If requirements are ambiguous, identify the ambiguity before making a major architectural decision.

---

## 31. Development Status

PHASE 0 - ENVIRONMENT

Completed:

- Python environment
- uv environment
- Google ADK
- pytest
- Google Cloud CLI
- Google Cloud authentication
- Google Cloud project
- Hackathon registration
- GEAR registration
- Hackathon credit request

Not completed:

- Production cloud deployment
- Final product name
- Final product scope
- Final architecture
- Final demo scenario

---

## 32. Next Phase

PHASE 1 - ARCHITECTURE

Required outputs:

1. Product definition
2. Exact MVP scope
3. Execution state model
4. Tool architecture
5. Agent architecture
6. Recovery architecture
7. Memory strategy
8. Local architecture
9. Cloud architecture
10. Testing strategy
11. Demo scenario

Do not begin large-scale implementation until these are reviewed.

---

## 33. North Star

The final product should make a judge think:

"This is not just an LLM responding to me.

It actually executes work, knows what happened, detects when something went wrong, and can recover without me babysitting it."

Every engineering decision should move the project toward that result.
