# Kernorq — Video Demo Script (3:55)

> **Goal:** Prove to a judge in <4 minutes that Kernorq is a real autonomous execution system — not a chatbot wrapper — that can take an outcome, create a plan, execute it, survive a failure, recover safely, and finish with verified evidence, running on Google Cloud.

**Recording setup**
- Resolution: 1920x1080, 30fps, system audio + mic
- Environment: clean checkout, `demo/workloads/golden_demo.csv` (22 tasks), Cloud Run service `kernorq-00006-p6w` live
- Failure mode: deterministic demo flag `KERNORQ_DEMO_FAILURE=timeout` — makes the 4th task’s first verification timeout (UNKNOWN state). Production default is OFF.
- Total runtime: 3:55 (leaves 5s buffer under 4:00 limit)

---

### Cast & Preconditions

| Item | Value |
|------|-------|
| Objective typed live | `Get this project ready for submission.` |
| Workload shown | `demo/workloads/golden_demo.csv` — 22 tasks, priority/deadline/dependencies |
| Execution engine | Existing orchestrator → executor → verifier → recovery (no second engine) |
| State store | SQLite (`executions.db`, WAL), thread-safe, checkpoints |
| Model | `gemini-3.5-flash` via Google ADK (only for planning) |
| Cloud | `https://kernorq-937607726293.us-central1.run.app` — Cloud Run, Artifact Registry, Secret Manager |

**Before recording checklist:** `uv run pytest -q` → 329 passed, 2 skipped. Workload preview shows 22 tasks, `planned_order` matches dependency-aware schedule. Cloud Run service URL reachable.

---

### Timeline — Visual | Narration (speak verbatim) | On-screen proof

**0:00–0:18 — Opening: The problem (show dashboard, no narration over music yet)**
- *Visual:* Static on Kernorq home: hero `KERNORQ — Turn workloads into verified outcomes` + Project `Kernorq Demo Project` / Workload `Golden Demo Workload — 22 tasks` + workload table (readable, priority pills).
- *Narration (0:08):* “AI assistants can tell us what to do. Someone still has to coordinate the work. We built Kernorq to take the outcome — not the checklist.”
- *Proof on screen:* No fake data — table rendered from `GET /workloads/golden` (22 rows, `TaskID`/`Category`/`DueDate`/`Priority`).

**0:18–0:32 — Give the outcome**
- *Action:* Cursor types in composer: `Get this project ready for submission.` → clicks `Execute Objective →`.
- *Visual:* Objective banner appears instantly, `execution_id: exec_…` visible, status pill `STARTING`.
- *Narration:* “I give it the outcome. Not the steps. Not the task list.”
- *Proof:* Network tab shows `POST /executions 201` with that objective — no hidden workflow.

**0:32–0:52 — Autonomous planning (no human steps)**
- *Visual:* `PLAN` panel expands: `PROJECT DELIVERY PLAN` — 6–7 steps generated live:
  `1 Inspect repository · 2 Validate requirements · 3 Run tests · 4 Resolve missing requirement · 5 Regenerate artifact · 6 Re-run verification · 7 Produce evidence`
- *Narration:* “Kernorq inspects the repo, discovers requirements, and generates its own plan. No template — the 22-task workload was the input, priority and deadline decided the order.”
- *Proof:* Plan JSON visible in expanded `Execution details` — `tool_name` values are only registered tools (`inspect_project_workspace`, `run_test_suite`, `project_diagnostics`, `research_topic`).

**0:52–1:32 — Real autonomous execution (user hands off)**
- *Visual:* `KERNORQ IS ORCHESTRATING` hero card — `Current task: 13 — Plan weekly workload • Priority HIGH • Due Aug 26 • EXECUTING` + `WHY THIS TASK? ✓ READY · Highest rank · Deadline considered · Dependencies satisfied` + live `READY TASKS` list + `EXECUTION TIMELINE` (`13 ✓ COMPLETED → 3 ✓ COMPLETED → 6 ✓ COMPLETED → 4 ▶ EXECUTING`…) — all derived from `GET /executions/{id}/tasks`.
- *Narration:* “Now it works alone. Every task is a real tool call — not a simulation. You can see it ranking READY tasks — it never runs a blocked task just because its priority is higher.”
- *Proof:* Show `SCHEDULING POLICY: Priority ✓ Deadline ✓ Dependencies ✓` and that `B(5, depends on A)` waits behind `A` even though `B` outranks others. Timeline icons come from backend `event_type`.

**1:32–1:48 — Persistent state**
- *Visual:* Hover `EXECUTION PIPELINE` → `Execution ID: exec_415544456154`, `Status: EXECUTING`, `Completed: task_001, task_002, task_003 / Current: task_004 / Pending: task_005…` + `CHECKPOINT_CREATED` events in timeline.
- *Narration:* “Every step is persisted — execution ID, checkpoints, task results. If the browser closes, the work continues. This is not chat history.”
- *Proof:* `GET /executions/{id}` snapshot visible; `checkpoints` array growing.

**1:48–2:14 — Controlled failure: timeout → UNKNOWN**
- *Visual:* Task 4 `VERIFYING…` → `✕ Verification Failed — Timeout while verifying operation.` Status pill `VERIFYING`, `Task 4: VERIFYING → FAILED`.
- *Narration:* “We inject one deterministic timeout — the kind that happens in production. The verifier does not guess. It marks the outcome UNKNOWN: did the operation actually complete?”
- *Proof:* `VERIFICATION_FAILED` event metadata shows `is_unknown: true`, `error_type: TimeoutError`. No manual “Retry” button pressed.

**2:14–2:32 — Unknown-state handling (the differentiator)**
- *Visual:* `Recovery` card appears: `Execution state uncertain. Did the previous operation actually complete? Checking external state… → Operation not completed. Retry is safe.`
- *Narration:* “Most agents would blindly retry — or give up. Kernorq asks the only safe question: what does the external state say? If the operation had succeeded, it would mark it complete without a second write — idempotency, not a duplicate task.”
- *Proof:* `RECOVERY_STARTED {category: UNKNOWN}`, `RECOVERY_SELECTED {recovery_action: RETRY, external_state: NOT_FOUND}`, `operation_id` unchanged across attempts.

**2:32–2:48 — Autonomous recovery**
- *Visual:* `↻ Recovery: RETRY — Attempt 2 / 3` + `TASK STARTED` for same `operation_id: a1b2c3…` (first 12 chars shown, stable) + `VERIFICATION PASSED` on second try.
- *Narration:* “It recovers alone. Same operation ID — no duplicate upload, no duplicate notification. Bounded retry — never infinite.”
- *Proof:* `attempt_count: 1 → 2`, `max_attempts: 3`, `recovery_history` shows one entry, `operation_id` identical.

**2:48–2:58 — Continue execution (same run, not a restart)**
- *Visual:* `5 Generate final artifact ✓ SUCCESS → 6 Run final verification ✓ SUCCESS` appear in the SAME timeline, same `execution_id`.
- *Narration:* “Same execution, continued — not a manual restart.”
- *Proof:* Timeline shows `RETRY_STARTED` inside the same event stream, no new `execution_id`.

**2:58–3:20 — Verified completion**
- *Visual:* Full-screen `KERNORQ — WORKLOAD COMPLETED` — `✓ 22 / 22 tasks completed` (or `6/6` for the short demo workload, depending on which objective you record — pick one and keep it consistent) + `✓ Priority scheduling ✓ Deadline-aware ✓ Dependency enforcement ✓ Real tool execution ✓ Verification complete` + `Execution order: 13 → 17 → 18 → 3 → 6 → …` (from `planned_order`/`completed_at`), `Execution ID: exec_…` + `TEST SUITE: 12 passed · 0 failed` + `RESEARCH FINDINGS` / `COMPETITOR BOARD` / `CAROUSEL: 5 slides` — each with `LIVE` vs `Demo fallback` badge.
- *Narration:* “Twenty-two tasks, scheduled by priority and deadline, respecting dependencies. Research is real findings, competitors are real analysis, the carousel is a real 5-slide artifact with hook, slides, CTA and caption — all verified. No task ran before its dependency.”
- *Proof:* Expand `Verification evidence` — `verification_results: 22`, each `verified_success`. `workload_summary` counts: `COMPLETED 22, FAILED 0`. For fallback tasks, badge reads `Demo fallback — deterministic` (honest, not masquerading as live).

**3:20–3:42 — Architecture + Cloud proof (keep short)**
- *Visual:* Quick overlay diagram + live Cloud Console:
  `USER → WEB UI → CLOUD RUN (kernorq-00006-p6w) → ADK AGENT (Planner → Executor → Verifier → Recovery) → SQLite`  (Firestore in prod, SQLite locally — note it)
  Then `gcloud run services describe kernorq --region us-central1` showing `Service URL: https://kernorq-…run.app`, `Image: us-central1-docker.pkg.dev/…:681c69f…`, `Traffic: 100%`. Briefly scroll Cloud Logging showing `EXECUTION_COMPLETED`.
- *Narration:* “LLM decides what should happen. Deterministic code decides how it happens — and persists it. All running on Cloud Run, image built via Cloud Build, secrets via Secret Manager.”
- *Proof:* Must be screen recording of real Console, not a static image.

**3:42–3:55 — Closing (end on verified state)**
- *Visual:* Freeze on `WORKLOAD COMPLETED` card.
- *Narration (verbatim):* “Give it the outcome. It discovers the work. It executes the work. It verifies the work. When the first plan fails, it recovers. You do not babysit the workflow.”
- *Hold 3s, fade to Kernorq URL and execution ID.*

---

### Demo Rules & Guards

- Language: English (no subtitles needed if narration is clear).
- No fake logs, no scripted typing animations, no manual workflow advancement.
- Failure is deterministic: `generate_carousel/research_topic` only use fallback when Gemini returns 404/no key — never random.
- Production flag OFF: `KERNORQ_DEMO_FAILURE` not set in Cloud Run env.
- `uv run pytest -q` before recording: 329 passed, 2 skipped (baseline). `scripts/run_demo_workload.py` → `completed 22/22`.
- Verify no contradictory state: `WORKLOAD COMPLETED` only when `exec.status === 'COMPLETED' && completed === total && failed === 0` (frontend already enforces this).

### Scoring Alignment (callouts during narration)

- *40% Innovation/Operational Utility:* “Human coordination eliminated — 22 tasks, 0 manual steps.”
- *30% Architectural Discipline:* “Modular planner/executor/verifier/recovery, persistent state, bounded retries, idempotency via operation_id.”
- *30% Demo/Production Readiness:* “Real execution, real failure, real recovery, reproducible, visible Cloud Run.”

### Recording Checklist

- [ ] `demo/workloads/golden_demo.csv` present in image (`COPY demo ./demo` in Dockerfile)
- [ ] `GET /workloads/golden` returns 22 tasks, `planned_order` dependency-aware
- [ ] `POST /workloads/golden/run` → `COMPLETED 22/22` via real tools (fallback badges honest)
- [ ] `GET /executions/{id}/events` shows `TASK_STARTED → CHECKPOINT → VERIFICATION → RECOVERY → RETRY` in one stream
- [ ] Cloud Run service URL reachable, logs show `EXECUTION_COMPLETED`
- [ ] Audio levels tested, narration English, <4:00, export 1080p MP4 for YouTube/Vimeo

### Appendix — Commands for the recording machine

```bash
uv run pytest -q
# → 329 passed, 2 skipped

uv run python scripts/run_demo_workload.py
# → planned order: ['13','3','6','4','17','18', ...] — 22/22 COMPLETED

gcloud run services describe kernorq --region us-central1 --format="value(status.url)"
# → https://kernorq-937607726293.us-central1.run.app

curl -X POST https://kernorq-.../workloads/golden/run
# → {"execution_id":"exec_…","status":"EXECUTING","planned_order": [...]}

# In UI: click ▶ Run Workload → watch KERNORQ IS ORCHESTRATING → WORKLOAD COMPLETED
```
