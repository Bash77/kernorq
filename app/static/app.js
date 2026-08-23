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

function renderPipeline(status) {
  const order = ['PLANNING','EXECUTING','VERIFYING','RECOVERING','COMPLETED','FAILED'];
  document.querySelectorAll('.step').forEach(s => {
    const st = s.dataset.step;
    s.classList.remove('active','done');
    if (st === status) s.classList.add('active');
    else if (order.indexOf(st) < order.indexOf(status)) s.classList.add('done');
    s.querySelector('.icon').textContent = s.classList.contains('done') ? '✓' : s.classList.contains('active') ? '●' : '○';
  });
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
      div.innerHTML = `<div class="eid">${e.execution_id}</div><div class="obj">${e.objective}</div><span class="st st-${e.status}">${e.status}</span>`;
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

async function loadDetail(id) {
  try {
    const [exec, tasks, events] = await Promise.all([api.getExecution(id), api.getTasks(id), api.getEvents(id)]);
    document.getElementById('exec-id').textContent = exec.execution_id;
    document.getElementById('exec-objective').textContent = exec.objective;
    setStatusBadge(document.getElementById('exec-status'), exec.status);
    renderPipeline(exec.status);
    document.getElementById('global-status').textContent = '● ' + exec.status;
    // tasks
    const tl = document.getElementById('task-list');
    tl.innerHTML = '';
    Object.values(tasks).forEach(t => {
      const d = document.createElement('div');
      d.className = 'task';
      const ver = t.verification ? `${t.verification.status} — ${t.verification.message}` : 'pending';
      const rec = exec.recovery_history?.find(r => r.task_id === t.task_id);
      d.innerHTML = `<div class="head"><span class="title">${t.task_id} — ${t.title}</span><span class="st st-${t.status}">${t.status}</span></div>
        <div class="meta">tool: ${t.tool_name||'—'} • attempts: ${t.attempt_count}/${t.max_attempts} • op: ${t.operation_id?.slice(0,8)}</div>
        <div class="meta">verification: ${ver}</div>
        ${rec ? `<div class="meta">recovery: ${rec.recovery_action} (${rec.reason})</div>` : ''}`;
      tl.appendChild(d);
    });
    // verification / recovery
    document.getElementById('verification').textContent = JSON.stringify(exec.verification_results, null, 2) || '—';
    document.getElementById('recovery').textContent = JSON.stringify(exec.recovery_history, null, 2) || '—';
    // timeline
    const tm = document.getElementById('event-timeline');
    tm.innerHTML = '';
    events.slice().reverse().forEach(ev => {
      const row = document.createElement('div');
      row.className = 'evt';
      const ts = new Date(ev.timestamp).toLocaleTimeString();
      row.innerHTML = `<span class="time">${ts}</span> <strong>${ev.event_type}</strong> ${ev.task_id?`— ${ev.task_id}`:''} <span style="color:#8a90a6">${ev.actor||''}</span>`;
      tm.appendChild(row);
    });
    document.getElementById('checkpoints').textContent = (exec.checkpoints||[]).map(c=> `${c.timestamp} — ${c.reason} ${c.task_id||''}`).join('\n') || '—';
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
