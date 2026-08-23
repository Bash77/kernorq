const api = {
  async createExecution(objective, llm_output) {
    const res = await fetch('/executions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({objective, llm_output}) });
    if (!res.ok) { const t = await res.text(); throw new Error(t); }
    return res.json();
  },
  async listExecutions() {
    const res = await fetch('/executions');
    if (!res.ok) throw new Error('list failed');
    return res.json();
  },
  async getExecution(id) { const r = await fetch(`/executions/${id}`); if (!r.ok) throw new Error('get failed'); return r.json(); },
  async getTasks(id) { const r = await fetch(`/executions/${id}/tasks`); if (!r.ok) throw new Error('tasks failed'); return r.json(); },
  async getEvents(id) { const r = await fetch(`/executions/${id}/events`); if (!r.ok) throw new Error('events failed'); return r.json(); },
};

let currentId = null;
let pollTimer = null;

function setStatusBadge(el, status) {
  el.textContent = status;
  el.className = 'status-badge st-' + status;
}

// Derive pipeline visualization from actual events and execution status — never invent
function renderPipeline(exec, events) {
  const types = new Set(events.map(e => e.event_type));
  const has = (t) => types.has(t);
  const status = exec.status;

  // Determine which lifecycle stages were actually reached
  const reached = {
    PLANNING: true, // execution exists → planning done
    EXECUTING: has('TASK_STARTED') || has('TOOL_EXECUTED') || status !== 'PENDING',
    VERIFYING: has('VERIFICATION_STARTED'),
    RECOVERING: has('RECOVERY_STARTED'),
    COMPLETED: status === 'COMPLETED',
    FAILED: status === 'FAILED',
  };

  // Determine verification outcome for icon
  const verFailed = has('VERIFICATION_FAILED');
  const verSucceeded = has('VERIFICATION_SUCCEEDED');
  const recoverySelected = events.find(e => e.event_type === 'RECOVERY_SELECTED');

  const steps = [
    { key: 'PLANNING', label: 'Planning', reached: reached.PLANNING, done: reached.PLANNING },
    { key: 'EXECUTING', label: 'Executing', reached: reached.EXECUTING, done: reached.EXECUTING },
    { key: 'VERIFYING', label: 'Verifying', reached: reached.VERIFYING, done: verSucceeded, failed: verFailed && !verSucceeded },
    { key: 'RECOVERING', label: 'Recovering', reached: reached.RECOVERING, done: reached.RECOVERING && (status === 'COMPLETED' || status === 'FAILED' || has('RETRY_STARTED')) },
    { key: 'COMPLETED', label: 'Completed', reached: reached.COMPLETED || reached.FAILED, done: reached.COMPLETED, failed: reached.FAILED },
  ];

  const container = document.getElementById('pipeline');
  container.innerHTML = '';
  steps.forEach(s => {
    const div = document.createElement('div');
    div.className = 'step';
    let icon = '○';
    if (!s.reached) {
      div.classList.add('pending');
      icon = '○';
    } else if (s.failed) {
      div.classList.add('failed');
      icon = '✕';
    } else if (s.done || s.reached) {
      // For RECOVERING, only mark done if it actually happened and completed; otherwise pending
      if (s.key === 'RECOVERING' && !has('RECOVERY_STARTED')) {
        div.classList.add('pending');
        icon = '○';
      } else {
        div.classList.add('done');
        icon = '✓';
      }
    }
    if (s.key === status) div.classList.add('active');
    // Special: FAILED as active when status FAILED
    if (status === 'FAILED' && s.key === 'COMPLETED') {
      div.textContent = ''; // hide completed when failed
      return;
    }
    if (status === 'COMPLETED' && s.key === 'FAILED') return;
    div.innerHTML = `<span class="icon">${icon}</span> ${s.label}`;
    container.appendChild(div);
  });

  // Add recovered indicator if applicable
  if (has('RECOVERY_STARTED')) {
    const rec = document.createElement('div');
    rec.className = 'pipeline-note';
    const action = recoverySelected?.metadata?.recovery_action || 'RETRY';
    rec.textContent = `Recovered via ${action} — ${events.filter(e=>e.event_type==='TASK_STARTED').length} attempts`;
    container.appendChild(rec);
  }
}

async function refreshList() {
  try {
    const list = await api.listExecutions();
    const container = document.getElementById('execution-list');
    container.innerHTML = '';
    if (!list.length) { container.innerHTML = '<div class="hint">No executions yet.</div>'; return; }
    list.forEach(e => {
      const div = document.createElement('div');
      div.className = 'exec-item' + (e.execution_id === currentId ? ' active' : '');
      // Distinguish recovered/failed/success via recovery_history and status
      let extra = '';
      if (e.recovery_history && e.recovery_history.length) {
        const last = e.recovery_history[e.recovery_history.length-1];
        extra = `<span class="st st-RECOVERING">RECOVERED ${last.recovery_action}</span>`;
      }
      div.innerHTML = `<div class="eid">${e.execution_id}</div><div class="obj">${e.objective}</div><span class="st st-${e.status}">${e.status}</span> ${extra}`;
      div.onclick = () => selectExecution(e.execution_id);
      container.appendChild(div);
    });
  } catch (e) { document.getElementById('execution-list').textContent = 'Failed to load'; }
}

async function selectExecution(id) {
  currentId = id;
  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('execution-detail').classList.remove('hidden');
  await loadDetail(id);
  refreshList();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => loadDetail(id), 2000);
}

function renderVerification(exec) {
  const container = document.getElementById('verification');
  const results = exec.verification_results || [];
  if (!results.length) {
    container.innerHTML = '<div class="hint">No verification yet — pending execution.</div>';
    return;
  }
  const last = results[results.length-1];
  const isSuccess = last.status === 'verified_success';
  const isUnknown = last.status === 'unknown';
  let html = `<div class="ver-summary ${isSuccess ? 'ok' : isUnknown ? 'warn' : 'err'}">
    <strong>${isSuccess ? '✓ Verification succeeded' : isUnknown ? '○ Verification unknown' : '✕ Verification failed'}</strong><br>
    <span>${last.message || ''}</span><br>`;
  if (last.evidence) {
    const ev = last.evidence;
    if (ev.required_fields) html += `<span>Required: ${ev.required_fields.join(', ')}</span><br>`;
    if (ev.found_fields) html += `<span>Found: ${ev.found_fields.join(', ')}</span><br>`;
    if (ev.operation_id) html += `<span>Operation ID: <code>${ev.operation_id.slice(0,12)}…</code></span><br>`;
  }
  html += `</div>`;
  html += `<details><summary>View verification evidence</summary><pre class="mono small">${JSON.stringify(results, null, 2)}</pre></details>`;
  container.innerHTML = html;
}

function renderRecovery(exec) {
  const container = document.getElementById('recovery');
  const hist = exec.recovery_history || [];
  if (!hist.length) {
    container.innerHTML = '<div class="hint">Recovery: None required — first attempt succeeded.</div>';
    return;
  }
  let html = '';
  hist.forEach(h => {
    const action = h.recovery_action || h.action || 'UNKNOWN';
    const reason = h.reason || h.category || '';
    const attempt = h.attempt ? `${h.attempt} → ${h.attempt+1}` : '';
    html += `<div class="rec-entry">
      <strong>Recovery: ${action}</strong> <span class="meta">Reason: ${reason}</span><br>
      <span class="meta">Attempt: ${attempt} • Task: ${h.task_id}</span><br>
      ${h.external_state ? `<span class="meta">External state: ${h.external_state}</span><br>` : ''}
      <span class="meta">Operation ID: <code>${(h.operation_id||'').slice(0,12)}…</code></span>
    </div>`;
  });
  // Distinguish final outcome
  const last = hist[hist.length-1];
  if (exec.status === 'COMPLETED' && last.recovery_action === 'RETRY') {
    html = `<div class="ver-summary ok"><strong>✓ Recovered via retry</strong> — verification succeeded on retry.</div>` + html;
  } else if (exec.status === 'COMPLETED' && last.recovery_action === 'FOUND_SUCCESS') {
    html = `<div class="ver-summary ok"><strong>✓ Recovered as success</strong> — external state FOUND, no duplicate.</div>` + html;
  } else if (exec.status === 'FAILED') {
    html = `<div class="ver-summary err"><strong>✕ Failed after recovery</strong> — ${last.reason}</div>` + html;
  }
  html += `<details><summary>View raw recovery history</summary><pre class="mono small">${JSON.stringify(hist, null, 2)}</pre></details>`;
  container.innerHTML = html;
}

async function loadDetail(id) {
  try {
    const [exec, tasks, events] = await Promise.all([api.getExecution(id), api.getTasks(id), api.getEvents(id)]);
    document.getElementById('exec-id').textContent = exec.execution_id;
    document.getElementById('exec-objective').textContent = exec.objective;
    setStatusBadge(document.getElementById('exec-status'), exec.status);
    renderPipeline(exec, events);
    document.getElementById('global-status').textContent = '● ' + exec.status;
    // tasks
    const tl = document.getElementById('task-list');
    tl.innerHTML = '';
    Object.values(tasks).forEach(t => {
      const ver = t.verification;
      let verText = 'pending';
      let verClass = '';
      if (ver) {
        verText = ver.status === 'verified_success' ? '✓ verified' : ver.status === 'unknown' ? '○ unknown' : '✕ failed';
        verClass = ver.status === 'verified_success' ? 'ok' : ver.status === 'unknown' ? 'warn' : 'err';
      }
      const rec = exec.recovery_history?.find(r => r.task_id === t.task_id);
      let recoveryLabel = '';
      if (rec) {
        recoveryLabel = rec.recovery_action === 'RETRY' ? `↻ RETRY (${rec.reason})` : rec.recovery_action;
      } else if (t.attempt_count > 1) {
        recoveryLabel = `↻ retried`;
      }
      const d = document.createElement('div');
      d.className = 'task';
      d.innerHTML = `<div class="head"><span class="title">${t.task_id} — ${t.title}</span><span class="st st-${t.status}">${t.status} ${recoveryLabel ? '• ' + recoveryLabel : ''}</span></div>
        <div class="meta">tool: ${t.tool_name||'—'} • attempts: ${t.attempt_count}/${t.max_attempts} • op: ${t.operation_id?.slice(0,8)}</div>
        <div class="meta ver-${verClass}">verification: ${verText} ${ver ? `— ${ver.message}` : ''}</div>
        ${t.error ? `<div class="meta err">error: ${t.error.type || ''} — ${t.error.message || ''}</div>` : ''}`;
      tl.appendChild(d);
    });
    renderVerification(exec);
    renderRecovery(exec);
    // timeline — chronological oldest → newest (backend is sole source, already ordered by timestamp)
    const tm = document.getElementById('event-timeline');
    tm.innerHTML = '';
    events.forEach(ev => {
      const row = document.createElement('div');
      row.className = 'evt';
      const ts = new Date(ev.timestamp).toLocaleTimeString();
      // Humanize event
      let label = ev.event_type;
      let extra = '';
      if (ev.event_type === 'TASK_STARTED') label = '▶ TASK_STARTED';
      else if (ev.event_type === 'VERIFICATION_STARTED') label = '○ VERIFICATION_STARTED';
      else if (ev.event_type === 'VERIFICATION_SUCCEEDED') label = '✓ VERIFICATION_SUCCEEDED';
      else if (ev.event_type === 'VERIFICATION_FAILED') label = '✕ VERIFICATION_FAILED';
      else if (ev.event_type === 'RECOVERY_STARTED') label = '↻ RECOVERY_STARTED';
      else if (ev.event_type === 'RECOVERY_SELECTED') label = `↻ RECOVERY_SELECTED → ${ev.metadata?.recovery_action||''}`;
      else if (ev.event_type === 'RETRY_STARTED') label = '↻ RETRY_STARTED';
      else if (ev.event_type === 'TASK_COMPLETED') label = '✓ TASK_COMPLETED';
      else if (ev.event_type === 'EXECUTION_COMPLETED') label = '✓ EXECUTION_COMPLETED';
      else if (ev.event_type === 'EXECUTION_FAILED') label = '✕ EXECUTION_FAILED';
      row.innerHTML = `<span class="time">${ts}</span> <strong>${label}</strong> ${ev.task_id?`— ${ev.task_id}`:''} <span style="color:#8a90a6">${ev.actor||''}</span>`;
      if (ev.metadata && Object.keys(ev.metadata).length) {
        const meta = document.createElement('div');
        meta.className = 'meta';
        meta.textContent = JSON.stringify(ev.metadata);
        row.appendChild(meta);
      }
      tm.appendChild(row);
    });
    document.getElementById('checkpoints').textContent = (exec.checkpoints||[]).map(c=> `${new Date(c.timestamp).toLocaleTimeString()} — ${c.reason} ${c.task_id||''}`).join('\n') || '—';
  } catch (e) { console.error(e); }
}

document.getElementById('objective-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const objective = document.getElementById('objective-input').value.trim();
  if (!objective) return;
  const btn = document.getElementById('run-btn');
  const err = document.getElementById('form-error');
  err.classList.add('hidden'); btn.disabled = true; btn.textContent = 'Running…';
  try {
    const res = await api.createExecution(objective);
    await refreshList();
    await selectExecution(res.execution_id);
  } catch (ex) {
    err.textContent = ex.message;
    err.classList.remove('hidden');
  } finally { btn.disabled = false; btn.textContent = 'Run objective'; }
});

// initial
refreshList();
setInterval(refreshList, 3000);
