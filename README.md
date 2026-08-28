# Kernorq

**Autonomous agentic workload orchestration engine** — give Kernorq an objective or workload, and it plans with Gemini, validates the plan deterministically, schedules dependent tasks, executes approved tools, verifies results with evidence, recovers from failures, and records an auditable execution trail.

> **LLM proposes. Kernorq validates. The deterministic engine executes and verifies.**

##  Live Demo

**Kernorq is running on Google Cloud Run:**

[Launch Kernorq Live Demo](https://kernorq-n7otynm6ea-uc.a.run.app/?utm_source=chatgpt.com)

The live application demonstrates:

* Natural-language objectives
* Voice input and output
* Gemini-powered planning
* Deterministic plan validation
* Workload prioritization
* Dependency-aware scheduling
* Tool execution
* Evidence-based verification
* Failure recovery
* Execution state and audit trail
* Multi-task workload orchestration

---

## What is Kernorq?

AI agents are increasingly good at completing **individual tasks**, but real-world work is rarely a single task.

A real workload can contain dozens of tasks with different:

* priorities
* deadlines
* dependencies
* tools
* execution constraints
* failure modes

Kernorq treats this as an **orchestration problem**, not simply a prompting problem.

Instead of asking an LLM to directly execute everything, Kernorq creates a controlled execution pipeline:

```text
Objective / Workload
        ↓
Gemini Planning
        ↓
Deterministic Validation
        ↓
Prioritization
        ↓
Dependency-Aware Scheduling
        ↓
Execution Orchestrator
        ↓
Tools / Research / Artifacts
        ↓
Evidence-Based Verification
        ↓
Recovery / Replanning
        ↓
Verified Outcome
```

The result is an agentic system designed around **controlled, observable, and recoverable execution**.

---

# Google Agent & Cloud Stack

Kernorq uses Google's agent and cloud technologies as core components of the system.

| Technology                             | Role in Kernorq                                              |
| -------------------------------------- | ------------------------------------------------------------ |
| **Google ADK (Agent Development Kit)** | Core agent framework and root agent definition               |
| **Gemini 3.5 Flash**                   | Planning, reasoning, intent understanding, and transcription |
| **Google GenAI SDK**                   | Gemini API integration                                       |
| **Gemini 3.1 Flash TTS Preview**       | Text-to-speech                                               |
| **Gemini 3.1 Flash Live Preview**      | Real-time live voice interaction                             |
| **Google Cloud Run**                   | Production hosting for the Kernorq application               |
| **Cloud Build**                        | Production container build pipeline                          |
| **Artifact Registry**                  | Stores the Kernorq container image                           |
| **Secret Manager**                     | Secure runtime injection of `GOOGLE_API_KEY`                 |
| **Cloud Logging**                      | Production application/runtime logging                       |
| **Vertex AI**                          | Optional Gemini/Google Cloud authentication fallback         |
| **FastAPI**                            | HTTP API and web application server                          |
| **SQLite**                             | Execution state, checkpoints, and audit persistence          |
| **Docker**                             | Containerized deployment                                     |

### Google ADK

Kernorq uses **Google ADK as its core agent framework**.

The root agent is defined using ADK's agent abstraction and uses Gemini as its reasoning model. ADK provides the agent layer while Kernorq's deterministic orchestration engine controls the actual execution lifecycle.

Conceptually:

```text
Google ADK Agent
       │
       │ Gemini reasoning
       ▼
Planning Proposal
       │
       ▼
Kernorq Deterministic Engine
       │
       ├── Validation
       ├── Prioritization
       ├── Scheduling
       ├── Execution
       ├── Verification
       └── Recovery
```

This separation is intentional:

> **ADK/Gemini handles agent intelligence. Kernorq controls execution.**

---

# Gemini Integration

Gemini is used throughout Kernorq for agent intelligence and voice capabilities.

### Planning & Reasoning

**Gemini 3.5 Flash** generates the initial planning proposal from the user's objective or workload.

The proposal is treated as **untrusted input**.

Kernorq then validates it before execution.

### Speech-to-Text

Gemini 3.5 Flash is used for transcription of spoken objectives.

### Text-to-Speech

Kernorq uses:

```text
gemini-3.1-flash-tts-preview
```

for spoken responses.

### Live Voice

Kernorq also supports real-time voice interaction through:

```text
gemini-3.1-flash-live-preview
```

---

# Architecture

```text
                         ┌─────────────────────────┐
                         │       USER              │
                         │                         │
                         │  Objective / Workload   │
                         │      Text / Voice       │
                         └────────────┬────────────┘
                                      │
                                      ▼
                         ┌─────────────────────────┐
                         │       GOOGLE ADK        │
                         │                         │
                         │     Root Agent          │
                         │          +              │
                         │     Gemini 3.5 Flash    │
                         └────────────┬────────────┘
                                      │
                              Planning Proposal
                                (Untrusted)
                                      │
                                      ▼
                    ┌─────────────────────────────────┐
                    │   DETERMINISTIC VALIDATION      │
                    │                                 │
                    │  • Plan sanitization            │
                    │  • Tool validation              │
                    │  • Dependency validation        │
                    │  • Cycle detection              │
                    │  • Execution constraints        │
                    └───────────────┬─────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────────┐
                    │       WORKLOAD PLANNER          │
                    │                                 │
                    │ Priority • Deadlines •          │
                    │ Dependencies • Task readiness  │
                    └───────────────┬─────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────────┐
                    │       WORKLOAD SCHEDULER        │
                    │                                 │
                    │ Determines which READY task    │
                    │ should execute next             │
                    └───────────────┬─────────────────┘
                                    │
                                    ▼
              ┌────────────────────────────────────────────┐
              │          EXECUTION ORCHESTRATOR            │
              │                                            │
              │      Deterministic State Machine           │
              │                                            │
              │ READY → RUNNING → VERIFYING → COMPLETED   │
              └───────────────┬────────────────────────────┘
                              │
                 ┌────────────┼────────────┐
                 │            │            │
                 ▼            ▼            ▼
           ┌──────────┐ ┌──────────┐ ┌────────────┐
           │  TOOLS   │ │ RESEARCH │ │ ARTIFACTS  │
           │          │ │          │ │            │
           │ Approved │ │ External │ │ Generated  │
           │ actions  │ │ research │ │ outputs    │
           └────┬─────┘ └────┬─────┘ └─────┬──────┘
                │            │             │
                └────────────┼─────────────┘
                             │
                             ▼
                    ┌─────────────────────┐
                    │      VERIFIER       │
                    │                     │
                    │ Evidence-based      │
                    │ SUCCESS / FAILURE / │
                    │ UNKNOWN             │
                    └──────────┬──────────┘
                               │
                     ┌─────────┴─────────┐
                     │                   │
                  SUCCESS              FAILURE
                     │                   │
                     │                   ▼
                     │          ┌─────────────────┐
                     │          │    RECOVERY     │
                     │          │                 │
                     │          │ Retry / Recover│
                     │          │ / Replan       │
                     │          └────────┬────────┘
                     │                   │
                     │                   └───────┐
                     │                           │
                     └───────────────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │    VERIFIED OUTCOME     │
                    │                         │
                    │ Result + Evidence +     │
                    │ Audit Trail             │
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │     SQLITE PERSISTENCE   │
                    │                         │
                    │ State • Checkpoints •   │
                    │ Execution Events        │
                    └─────────────────────────┘
```

---

# The Core Design Principle

Kernorq separates **reasoning** from **execution control**.

The LLM is powerful but probabilistic. Execution therefore does not depend on the LLM being correct.

```text
                 LLM / Gemini
                     │
                     │ proposes
                     ▼
              ┌───────────────┐
              │   Kernorq     │
              │   Validation  │
              └───────┬───────┘
                      │
                 approved plan
                      │
                      ▼
              Deterministic Engine
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Execute     Verify      Recover
          │           │           │
          └───────────┼───────────┘
                      ▼
               Verified Outcome
```

This gives Kernorq a clear trust boundary:

> **Gemini proposes what should happen. Kernorq determines what is allowed to happen.**

---

# Workload Orchestration

Kernorq is designed to orchestrate **workloads**, not just individual prompts.

A workload can contain:

```text
Task
├── Priority
├── Deadline
├── Dependencies
├── Required tools
└── Expected outcome
```

The scheduler uses workload constraints to determine which task should execute next.

Tasks blocked by dependencies remain pending until their prerequisites are completed.

This means the execution order is **determined by workload state**, rather than simply following the order in which tasks appear in a CSV.

---

# Golden Demo Workload

Kernorq includes a canonical **22-task Golden Demo workload**.

```text
demo/workloads/golden_demo.csv
```

The workload demonstrates:

* multi-task planning
* priorities
* deadlines
* dependency chains
* dependency-aware scheduling
* research operations
* tool execution
* artifact generation
* verification
* recovery
* complete workload execution

The backend remains authoritative over execution. The UI visualizes the actual execution state rather than simulating it.

This demonstrates Kernorq's central goal:

> **Turn workloads into verified outcomes.**

---

# Verification & Recovery

Kernorq does not consider an LLM response to be proof of successful execution.

Every executable task can pass through evidence-based verification.

Verification produces:

```text
SUCCESS
FAILURE
UNKNOWN
```

Failures are classified by the recovery subsystem:

```text
TRANSIENT
UNKNOWN
PERMANENT
```

Depending on the failure classification, Kernorq can:

* retry safe/idempotent operations
* recover from transient failures
* preserve execution state
* stop unsafe operations
* replan remaining work when appropriate

Execution checkpoints and audit events are persisted in SQLite.

---

# Google Cloud Deployment

Kernorq is deployed as a containerized production application on **Google Cloud Run**.

The production pipeline is:

```text
                         Source
                           │
                           ▼
                    Google Cloud Build
                           │
                           ▼
                     Docker Image
                           │
                           ▼
                  Artifact Registry
                           │
                           ▼
                     Cloud Run
                           │
              ┌────────────┴────────────┐
              │                         │
              ▼                         ▼
        Kernorq API/UI            Cloud Logging
              │
              ▼
       Google Secret Manager
              │
              │ GOOGLE_API_KEY
              ▼
            Gemini
```

### Secret Management

The Gemini API key is stored in **Google Cloud Secret Manager**.

Cloud Run receives the secret at runtime:

```text
Secret Manager
      │
      │ GOOGLE_API_KEY
      ▼
  Cloud Run
      │
      ▼
 Gemini API
```

The API key is **not committed to Git** and is **not baked into the Docker image**.

### Deployment Components

The repository includes:

```text
Dockerfile
cloudbuild.yaml
deploy/
```

for the Cloud Run deployment workflow.

---

# Optional Vertex AI Support

Kernorq also supports a Google Cloud/Vertex AI authentication path when a Gemini Developer API key is not provided.

The application can use:

```text
GOOGLE_CLOUD_PROJECT
GOOGLE_CLOUD_LOCATION
GOOGLE_GENAI_USE_VERTEXAI=true
```

with Google Application Default Credentials.

This allows Kernorq to operate through Google's cloud-native authentication model in addition to the Gemini Developer API path used for local development.

---

# Local Development

## Requirements

* Python **3.12+**
* [`uv`](https://docs.astral.sh/uv/)
* Gemini API key from Google AI Studio

## Install

```powershell
uv sync
```

## Configure

```powershell
Copy-Item .env.example .env
```

Edit `.env` and provide:

```text
GOOGLE_API_KEY=<your key>
```

**Never commit `.env` or API credentials.**

## Run

```powershell
uv run uvicorn app.api.main:app --reload --port 8000
```

Open:

```text
http://localhost:8000
```

---

# Environment Variables

| Variable                   | Purpose                                  | Default                         |
| -------------------------- | ---------------------------------------- | ------------------------------- |
| `GOOGLE_API_KEY`           | Gemini Developer API authentication      | —                               |
| `MODEL_NAME`               | Agent/planner model; must be Gemini 3.5+ | `gemini-3.5-flash`              |
| `KERNORQ_TRANSCRIBE_MODEL` | Speech transcription model               | `gemini-3.5-flash`              |
| `KERNORQ_TTS_MODEL`        | Text-to-speech model                     | `gemini-3.1-flash-tts-preview`  |
| `KERNORQ_LIVE_MODEL`       | Live voice model                         | `gemini-3.1-flash-live-preview` |
| `GEMINI_FALLBACK`          | Demo fallback when Gemini is unavailable | `true`                          |

---

# Testing

Run the complete test suite:

```powershell
python -m pytest -q
```

The test suite covers the core execution engine, persistence, workload planning, scheduling, verification, recovery, voice capabilities, UI behavior, deployment configuration, and regression cases.

---

# Project Structure

```text
kernorq/
├── app/
│   ├── agent/
│   │   ├── agent.py              # Google ADK root agent
│   │   ├── gemini_client.py     # Gemini integration
│   │   ├── intent.py             # Intent routing
│   │   ├── voice.py              # Voice/STT/TTS
│   │   └── live_voice.py         # Live voice
│   │
│   ├── api/                      # FastAPI application
│   ├── memory/                   # SQLite persistence
│   ├── orchestration/            # Execution engine
│   ├── tools/                    # Approved tools
│   ├── workload/                 # Workload planning/scheduling
│   └── static/                   # Web UI
│
├── demo/
│   └── workloads/
│       └── golden_demo.csv       # 22-task demo workload
│
├── deploy/                       # Cloud Run deployment
├── docs/                         # Architecture & execution docs
├── scripts/                      # Utility scripts
├── tests/                        # Automated tests
├── Dockerfile
├── cloudbuild.yaml
├── pyproject.toml
└── README.md
```

---

# Hackathon Technology Requirements

Kernorq uses the Google technologies required by the hackathon as **functional parts of the application**:

### Agent Framework

**Google ADK** is used as the core agent framework.

### Gemini

**Gemini 3.5 Flash** powers planning and reasoning through the Google GenAI SDK.

### Google Cloud

Kernorq is deployed on:

* **Cloud Run**
* **Cloud Build**
* **Artifact Registry**
* **Secret Manager**
* **Cloud Logging**

### Agentic Workflow

The project demonstrates a complete agentic workflow rather than a chatbot:

```text
Understand Objective
        ↓
Plan
        ↓
Validate
        ↓
Prioritize
        ↓
Schedule
        ↓
Execute
        ↓
Verify
        ↓
Recover / Replan
        ↓
Verified Outcome
```

---

# Documentation

Further technical documentation:

* `docs/ARCHITECTURE.md`
* `docs/EXECUTION_MODEL.md`
* `docs/DEMO.md`

---

## Kernorq in One Sentence

> **Kernorq turns complex workloads into verified outcomes by combining Gemini-powered planning with deterministic scheduling, execution, verification, and recovery.**
