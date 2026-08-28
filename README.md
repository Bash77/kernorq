# Kernorq

**Autonomous execution agent** — give it an objective; it plans with Gemini,
validates the plan deterministically, executes approved tools, verifies results
with evidence, recovers from failures safely, and records a complete audit trail.

> LLM proposes. Kernorq validates. The deterministic engine executes and verifies.

- Planner / reasoning / STT: **Gemini 3.5 Flash**
- TTS: **Gemini 3.1 Flash TTS Preview**
- Live voice mode: **Gemini 3.1 Flash Live Preview**
- Execution core: deterministic Python state machine (no LLM), SQLite persistence

## Requirements

- Python 3.12+
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)

## Local setup

```powershell
# 1. Install dependencies
uv sync

# 2. Configure environment — copy the template, then fill in GOOGLE_API_KEY
Copy-Item .env.example .env
# Edit .env: set GOOGLE_API_KEY=<your key>. Never commit this file.

# 3. Run the API + UI
uv run uvicorn app.api.main:app --reload --port 8000
```

Open http://localhost:8000 — type an objective or use voice mode.

### Environment variables

| Variable | Purpose | Required |
| --- | --- | --- |
| `GOOGLE_API_KEY` | Gemini Developer API key (planner, STT, chat) | Yes (or Vertex config) |
| `MODEL_NAME` | Planner model; must be `gemini-3.5`+ | No (default `gemini-3.5-flash`) |
| `KERNORQ_TRANSCRIBE_MODEL` | STT model | No (default `gemini-3.5-flash`) |
| `KERNORQ_TTS_MODEL` | TTS model | No (default `gemini-3.1-flash-tts-preview`) |
| `KERNORQ_LIVE_MODEL` | Live voice model | No (default `gemini-3.1-flash-live-preview`) |
| `GEMINI_FALLBACK` | Demo fallback when Gemini is unreachable | No (default `true`) |

## Run tests

```powershell
python -m pytest -q
```

## Deploy to Google Cloud Run

Prerequisites: gcloud CLI authenticated, project has **billing enabled**, and
you are signed in to the target project.

```powershell
gcloud services enable run.googleapis.com secretmanager.googleapis.com `
    cloudbuild.googleapis.com artifactregistry.googleapis.com
gcloud artifacts repositories create kernorq --repository-format=docker --location=us-central1

$env:GOOGLE_API_KEY = "<your key>"     # provided to Cloud Run via Secret Manager
./deploy/deploy-cloud-run.ps1
```

The script creates/updates Secret Manager secret `kernorq-google-api-key`,
builds via Cloud Build, deploys to Cloud Run (scale-to-zero, max 2 instances),
and injects the secret at runtime. The API key never enters Git or the image.

## Architecture

```
Objective (text/voice)
   → Gemini 3.5 Flash planning proposal      (untrusted)
   → sanitize + deterministic plan validation (tool/deps/cycles)
   → SQLite-persisted Execution
   → Orchestrator loop:
        Executor (approved tools only)
        Verifier (evidence-based SUCCESS/FAILURE/UNKNOWN)
        Recovery (TRANSIENT/UNKNOWN/PERMANENT; idempotent retry)
   → verified result + audit events/checkpoints
```

See `docs/ARCHITECTURE.md` and `docs/EXECUTION_MODEL.md`.
