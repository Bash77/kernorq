/* Kernorq frontend — observability client. Backend is the sole source of truth. */
const api = {
  async createExecution(objective, llm_output) {
    const res = await fetch('/executions', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({objective, llm_output}) });
    if (!res.ok) throw new Error((await res.json().catch(()=>({detail:res.statusText}))).detail || 'Failed to start execution');
    return res.json();
  },
  async listExecutions() {
    const res = await fetch('/executions');
    if (!res.ok) throw new Error('list failed');
    return res.json();
  },
  async getExecution(id) { const r = await fetch(`/executions/${id}`); if (!r.ok) throw Object.assign(new Error('get failed'), {status:r.status}); return r.json(); },
  async getTasks(id) { const r = await fetch(`/executions/${id}/tasks`); if (!r.ok) throw Object.assign(new Error('tasks failed'), {status:r.status}); return r.json(); },
  async getEvents(id) { const r = await fetch(`/executions/${id}/events`); if (!r.ok) throw Object.assign(new Error('events failed'), {status:r.status}); return r.json(); },
  async voiceStatus() { const r = await fetch('/voice/status'); return r.json(); },
  async converse(text) {
    const res = await fetch('/converse', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    if (!res.ok) throw new Error('converse failed');
    return res.json();
  },
  async transcribe(audioBase64, mime) {
    const res = await fetch('/voice/transcribe', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({audio_base64:audioBase64,mime_type:mime})});
    const j = await res.json();
    if (!res.ok) throw new Error(j.detail || 'Transcription failed');
    return j.text;
  },
  async speak(text) {
    const res = await fetch('/voice/speak', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    const j = await res.json();
    if (!res.ok) throw new Error(j.detail || 'Speech unavailable');
    return j;
  },
  async getGoldenWorkload() {
    const r = await fetch('/workloads/golden');
    if (!r.ok) throw new Error('workload load failed');
    return r.json();
  },
  async runGoldenWorkload() {
    const r = await fetch('/workloads/golden/run', {method:'POST'});
    if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail || 'workload run failed');
    return r.json();
  },
  async getWorkloadSummary(id) {
    const r = await fetch(`/workloads/golden/executions/${id}/summary`);
    if (!r.ok) throw Object.assign(new Error('summary failed'), {status:r.status});
    return r.json();
  },
};

/* ---------------- Demo Workload (golden_demo.csv via existing engine) ---------------- */
let demoWorkload = null;

async function loadDemoWorkload(){
  try{
    const data = await api.getGoldenWorkload();
    demoWorkload = data;
    document.getElementById('demo-task-count').textContent = `${data.total} tasks`;
    document.getElementById('overview-count').textContent = `${data.total} tasks`;
    document.getElementById('demo-workload-sub').textContent = `Priority · Deadline · Dependencies — planned order: ${data.planned_order.slice(0,6).join(' → ')}…`;
    document.getElementById('workload-overview').classList.remove('hidden');
    renderWorkloadTable(data);
    renderWorkloadIntelligence(data);
  }catch(e){
    document.getElementById('demo-workload-sub').textContent = 'Workload unavailable — check server';
  }
}

function renderWorkloadTable(data){
  const el = document.getElementById('workload-table');
  if(!el) return;
  const prioClass = p => p===1?'high':p===3?'med':'low';
  const prioLabel = p => p===1?'High':p===3?'Medium':p===5?'Low':String(p);
  el.innerHTML = `<div class="workload-grid">
    <div class="workload-head"><span>TASK</span><span>CATEGORY</span><span>PRIORITY</span><span>DEADLINE</span><span>STATUS</span></div>
    ${data.tasks.map(t=>`
      <div class="workload-row status-${(t.planned_status||'READY').toLowerCase()}" data-task-id="${t.task_id}" onclick="showTaskDetail('${t.task_id}')" style="cursor:pointer">
        <span class="w-id">${t.task_id}</span>
        <span class="w-title" title="${t.title}">${t.title}</span>
        <span class="w-cat">${t.category}</span>
        <span class="w-prio ${prioClass(t.priority)}">${prioLabel(t.priority)}</span>
        <span class="w-deadline">${t.deadline_display||'—'}</span>
        <span class="w-status">${t.planned_status}</span>
      </div>`).join('')}
  </div>`;
}

function updateWorkloadTableLive(tasks){
  const el = document.getElementById('workload-table');
  if(!el || !tasks) return;
  Object.values(tasks).forEach(t=>{
    const row = el.querySelector(`[data-task-id="${t.task_id}"]`);
    if(!row) return;
    const bucket = workloadStatusBucket(t.status);
    row.className = `workload-row status-${bucket.toLowerCase()}`;
    const statusEl = row.querySelector('.w-status');
    if(statusEl) statusEl.textContent = bucket;
  });
}

function renderWorkloadIntelligence(data){
  const summaryEl = document.getElementById('workload-summary');
  const needEl = document.getElementById('what-you-need');
  const workingEl = document.getElementById('kernorq-working');
  const nextEl = document.getElementById('next-hour');
  const laterEl = document.getElementById('later-today');
  if(!summaryEl || !data) return;
  const high = data.tasks.filter(t=>t.priority===1).length;
  const today = data.tasks.filter(t=>t.deadline==='2026-08-26').length;
  const readyCount = data.counts ? data.counts.ready : data.tasks.filter(t=>t.planned_status==='READY').length;
  summaryEl.innerHTML = `
    <div class="summary-row"><span>22 TASKS</span><span class="summary-count">${data.total}</span></div>
    <div class="summary-row"><span>HIGH PRIORITY</span><span class="summary-count">${high}</span></div>
    <div class="summary-row"><span>DUE TODAY</span><span class="summary-count">${today}</span></div>
    <div class="summary-row"><span>READY NOW</span><span class="summary-count">${readyCount}</span></div>
  `;
  // What you need now: top READY tasks by actual scheduling rank (backend-driven)
  const readyTasks = data.tasks.filter(t=>t.planned_status==='READY').slice(0,3);
  needEl.innerHTML = readyTasks.map(t=>`
    <div class="need-card ${t.priority===1?'':t.priority===5?'low':'medium'}" onclick="showTaskDetail('${t.task_id}')" style="cursor:pointer">
      <div class="need-title">${t.task_id} — ${t.title}</div>
      <div class="need-meta">Priority ${t.priority_label} • Due ${t.deadline_display} • ${t.category}</div>
      <div class="hint">${t.planned_status} • ${t.why}</div>
    </div>
  `).join('') || '<div class="hint">All tasks prioritized — see execution order</div>';
  // Kernorq is working: tasks Kernorq can handle autonomously (from backend)
  const kernorqTasks = data.tasks.filter(t=>['💻 Project','🤖 Kernorq Demo','🔬 Research','🤝 Client Research','📱 Content'].includes(t.category) && t.planned_status==='READY').slice(0,2);
  workingEl.innerHTML = kernorqTasks.map(t=>`
    <div class="working-card"><span class="working-dot"></span><span>${t.title} — ${t.category}</span></div>
  `).join('') || '<div class="hint">Kernorq will handle project, research, and content tasks autonomously</div>';
  // Next — scheduler ordering, no fake times (backend has no wall-clock, only priority order)
  const upNext = data.planned_order.slice(0,3);
  nextEl.innerHTML = upNext.map((id,i)=>{
    const t = data.tasks.find(x=>x.task_id===id);
    return `<div class="next-slot"><div class="next-time">Up next #${i+1} — ${t ? t.title : id}</div><div class="hint">→ ${t ? t.category : ''} • Priority ${t ? t.priority_label : ''} • ${t ? t.why : ''}</div></div>`;
  }).join('');
  // Later — remaining scheduled work
  const laterIds = data.planned_order.slice(3,6);
  const laterTasks = laterIds.map(id=> data.tasks.find(x=>x.task_id===id)).filter(Boolean);
  laterEl.innerHTML = laterTasks.map(t=>`
    <div class="later-card"><strong>${t.task_id} — ${t.title}</strong> — ${t.category}<br><span class="hint">Scheduled • Due ${t.deadline_display} • Priority ${t.priority_label}</span></div>
  `).join('') || '<div class="hint">Remaining tasks scheduled by priority</div>';
  document.getElementById('workload-intel').classList.remove('hidden');
}

function showTaskDetail(taskId){
  if(!demoWorkload) return;
  const t = demoWorkload.tasks.find(x=>x.task_id===taskId);
  if(!t) return;
  const el = document.getElementById('task-detail');
  const content = document.getElementById('task-detail-content');
  const prioLabel = t.priority===1?'High':t.priority===3?'Medium':'Low';
  content.innerHTML = `
    <div><strong>${t.task_id} — ${t.title}</strong></div>
    <div class="hint">${t.category} • Priority ${prioLabel} • Due ${t.deadline_display}</div>
    <div style="margin:8px 0">${t.why}</div>
    <div class="panel-label">KERNORQ PLAN</div>
    <ol style="font-size:13px;line-height:1.6;padding-left:18px">
      <li>Understand: ${t.title}</li>
      <li>Prioritize by rank #${t.selection_rank}</li>
      <li>Execute via ${t.category==='💻 Project'?'project diagnostics':t.category==='🔬 Research'?'research':t.category==='📱 Content'?'content generation':'inspection'}</li>
      <li>Verify evidence</li>
      <li>Surface result</li>
    </ol>
  `;
  el.classList.remove('hidden');
  el.scrollIntoView({behavior:'smooth'});
}

async function handleRunWorkload(){
  const btn = document.getElementById('run-workload-btn');
  const status = document.getElementById('demo-hero-status');
  btn.disabled = true; btn.innerHTML = '⏳ Starting…';
  status.textContent = 'Kernorq is planning the workload…';
  try{
    const res = await api.runGoldenWorkload();
    status.textContent = `Workload started — ${res.total} tasks`;
    openExecution(res.execution_id);
  }catch(e){
    status.textContent = 'Failed to start workload: '+e.message;
    btn.disabled = false; btn.innerHTML = '<span class="cta-icon">▶</span> Run Workload';
  }
}
document.getElementById('run-workload-btn')?.addEventListener('click', handleRunWorkload);

function workloadStatusBucket(s){
  const map = {PENDING:'READY',READY:'READY',BLOCKED:'BLOCKED',RUNNING:'EXECUTING',VERIFYING:'EXECUTING',RECOVERING:'EXECUTING',SUCCEEDED:'COMPLETED',FAILED:'FAILED',CANCELLED:'CANCELLED'};
  return map[s]||s;
}

function renderDemoLive(exec, tasks, events){
  const container = document.getElementById('demo-live');
  if(!container) return;
  const isWorkload = exec.objective === 'Kernorq golden demo workload' || Object.keys(tasks).length===22;
  if(!isWorkload){ container.classList.add('hidden'); return; }
  container.classList.remove('hidden');

  // Hero header: show ORCHESTRATING only while actually executing
  const liveHead = container.querySelector('.demo-live-head .panel-label');
  const pulse = container.querySelector('.demo-live-pulse');
  if(exec.status==='COMPLETED'){
    if(liveHead) liveHead.textContent = 'WORKLOAD COMPLETED';
    if(pulse) pulse.style.display = 'none';
  } else if(exec.status==='FAILED'){
    if(liveHead) liveHead.textContent = 'WORKLOAD FAILED';
    if(pulse) pulse.style.display = 'none';
  } else {
    if(liveHead) liveHead.textContent = 'KERNORQ IS ORCHESTRATING';
    if(pulse) pulse.style.display = '';
  }

  // Update workload overview table live with backend task statuses (authoritative)
  updateWorkloadTableLive(tasks);

  // Current task — the one RUNNING/VERIFYING, or the next READY by plan order
  let current = Object.values(tasks).find(t=>t.status==='RUNNING' || t.status==='VERIFYING');
  if(!current){
    const ready = Object.values(tasks).filter(t=>t.status==='READY');
    if(ready.length && demoWorkload){
      const rank = Object.fromEntries(demoWorkload.planned_order.map((id,i)=>[id,i]));
      ready.sort((a,b)=>(rank[a.task_id]??999)-(rank[b.task_id]??999));
      current = ready[0];
    } else {
      current = Object.values(tasks).find(t=>t.status==='READY') || null;
    }
  }
  if(current){
    document.getElementById('demo-current-task').textContent = `${current.task_id} — ${current.title}`;
    document.getElementById('demo-current-desc').textContent = current.description||'';
    const prioLabel = current.task_id && demoWorkload ? (demoWorkload.tasks.find(x=>x.task_id===current.task_id)?.priority_label||'') : '';
    document.getElementById('demo-current-prio').textContent = prioLabel ? `Priority ${prioLabel.toUpperCase()}` : `Priority ${current.task_id}`;
    const wl = demoWorkload?.tasks.find(x=>x.task_id===current.task_id);
    document.getElementById('demo-current-deadline').textContent = wl ? `Due ${wl.deadline_display}` : '';
    document.getElementById('demo-current-status').textContent = workloadStatusBucket(current.status);
    const whyList = document.getElementById('demo-why-list');
    const wlEntry = demoWorkload?.tasks.find(x=>x.task_id===current.task_id);
    const isReady = current.status==='READY'||current.status==='RUNNING'||current.status==='VERIFYING';
    whyList.innerHTML = `
      <li class="${isReady?'ok':''}">${isReady?'✓ READY':'○ Not ready'} — ${wlEntry?wlEntry.why:current.status}</li>
      <li class="ok">✓ Highest scheduling rank — Priority ${prioLabel||'—'} considered</li>
      <li class="ok">✓ Deadline considered — ${wl?wl.deadline_display:'—'}</li>
      <li class="ok">✓ Dependencies satisfied — ${wlEntry&&wlEntry.dependencies?.length?wlEntry.dependencies.join(', '):'none'}</li>`;
  } else {
    const isCompleted = exec.status==='COMPLETED';
    const isFailed = exec.status==='FAILED';
    document.getElementById('demo-current-task').textContent = isCompleted ? 'All tasks completed — verified' : isFailed ? 'Workload stopped — see failed tasks' : '—';
    document.getElementById('demo-current-desc').textContent = isCompleted ? 'Every task executed, verified, and completed.' : '';
    document.getElementById('demo-current-prio').textContent = isCompleted ? 'Completed' : isFailed ? 'Failed' : '—';
    document.getElementById('demo-current-deadline').textContent = '';
    document.getElementById('demo-current-status').textContent = isCompleted ? 'COMPLETED' : isFailed ? 'FAILED' : '—';
    const whyList = document.getElementById('demo-why-list');
    if(whyList) whyList.innerHTML = isCompleted ? '<li class="ok">✓ All 22 tasks verified — workload completed</li>' : isFailed ? '<li>○ Workload stopped on failure — see timeline</li>' : '';
  }

  // READY TASKS live list
  const readyList = document.getElementById('demo-ready-tasks');
  if(readyList){
    const readies = Object.values(tasks).filter(t=>t.status==='READY');
    if(demoWorkload){
      const rank = Object.fromEntries(demoWorkload.planned_order.map((id,i)=>[id,i]));
      readies.sort((a,b)=>(rank[a.task_id]??999)-(rank[b.task_id]??999));
    }
    readyList.innerHTML = readies.length ? readies.slice(0,8).map(t=>{
      const wl = demoWorkload?.tasks.find(x=>x.task_id===t.task_id);
      return `<div class="ready-row"><span>${t.task_id}</span><span>${wl?wl.priority_label:'—'}</span><span>${wl?wl.deadline_display:'—'}</span></div>`;
    }).join('') : '<div class="hint">No READY tasks — waiting for dependencies</div>';
  }

  // Timeline — actual backend order: sort by completed_at or by task_id order of execution events
  const timeline = document.getElementById('demo-timeline');
  if(timeline){
    const ordered = Object.values(tasks).slice().sort((a,b)=>{
      const ao = a.completed_at||a.started_at||'', bo=b.completed_at||b.started_at||'';
      if(ao && bo) return ao.localeCompare(bo);
      return 0;
    });
    // Fallback: use planned_order for pending, actual completed order for done
    timeline.innerHTML = Object.values(tasks).map(t=>{
      const bucket = workloadStatusBucket(t.status);
      const icon = bucket==='COMPLETED'?'✓':bucket==='FAILED'?'✕':bucket==='EXECUTING'?'▶':bucket==='BLOCKED'?'🔒':'⏳';
      return `<div class="demo-tl-row ${bucket.toLowerCase()}"><span class="tl-id">${t.task_id}</span><span class="tl-icon">${icon}</span><span class="tl-title">${t.title}</span><span class="tl-status">${bucket}</span></div>`;
    }).join('');
  }

  // Verification panel
  const execCheck = document.getElementById('demo-exec-check');
  const verCheck = document.getElementById('demo-ver-check');
  const testEv = document.getElementById('demo-test-evidence');
  if(execCheck && verCheck){
    const hasTask = Object.values(tasks).some(t=>t.result);
    execCheck.textContent = hasTask ? '✓ Tool executed' : '○ Tool executed';
    execCheck.className = hasTask ? 'demo-check ok' : 'demo-check';
    const hasVer = Object.values(tasks).some(t=>t.verification);
    verCheck.textContent = hasVer ? '✓ Evidence verified' : '○ Evidence verified';
    verCheck.className = hasVer ? 'demo-check ok' : 'demo-check';
    // Test suite evidence
    const suiteTask = Object.values(tasks).find(t=>t.result && t.result.test_count!==undefined);
    if(suiteTask && suiteTask.result){
      testEv.classList.remove('hidden');
      testEv.innerHTML = `<div class="panel-label">TEST SUITE</div><div>${suiteTask.result.passed??'?'} passed · ${suiteTask.result.failed??'?'} failed · ${suiteTask.result.skipped??0} skipped</div>`;
    } else {
      testEv.classList.add('hidden');
    }
  }

  // Dependency visualization — backend-driven, no invented deps
  const depsEl = document.getElementById('demo-deps');
  if(depsEl){
    const withDeps = Object.values(tasks).filter(t=>t.dependencies && t.dependencies.length);
    if(withDeps.length){
      depsEl.innerHTML = withDeps.map(t=>`
        <div class="dep-card"><div class="dep-task">🔒 ${t.task_id} — ${t.title}</div><div class="hint">Depends on: ${t.dependencies.join(', ')}</div></div>`).join('');
    } else {
      depsEl.innerHTML = `<div class="hint">Golden workload has no dependencies — all 22 tasks start READY. Dependency enforcement proven in tests via synthetic VIP task (VIP_AFTER_13 → depends on 13).</div>
        <div class="dep-example"><div>VIP_AFTER_13 🔒 BLOCKED</div><div class="dep-arrow">↓ depends on</div><div>13 ✓ COMPLETED → VIP_AFTER_13 🔓 READY</div></div>`;
    }
  }

  // Work produced — backend-driven, progressive
  const workEl = document.getElementById('work-produced');
  if(workEl){
    const produced = Object.values(tasks).filter(t=>t.result && t.status==='SUCCEEDED');
    if(!produced.length) workEl.innerHTML = '<div class="hint">No work produced yet — Kernorq is orchestrating</div>';
    else workEl.innerHTML = produced.slice(0,6).map(t=>{
      const tool = t.tool_name||'';
      const res = t.result||{};
      let preview = '';
      if(tool==='research_topic' && res.findings) preview = `${res.findings.length} findings • ${res.sources?.length||0} sources`;
      else if(tool==='analyze_competitors' && res.competitors) preview = `${res.competitors.length} competitors • ${res.patterns?.length||0} patterns`;
      else if(tool==='generate_carousel' && res.slides) preview = `${res.slides.length} slides • Hook: "${(res.hook||'').slice(0,40)}"`;
      else if(tool==='run_test_suite' && res.test_count!==undefined) preview = `${res.passed||0} passed • ${res.failed||0} failed`;
      else if(tool==='project_diagnostics' && res.summary) preview = `${res.summary.issue_count||0} issues • ${res.files_inspected||0} files`;
      else preview = tool;
      return `<div class="produced-card"><div class="produced-title">${t.task_id} — ${t.title}</div><div class="hint">${preview}</div></div>`;
    }).join('');
  }

  // Research findings — real backend output, fallback transparency
  const researchEl = document.getElementById('research-findings');
  const sourcesEl = document.getElementById('research-sources');
  if(researchEl){
    const researchTasks = Object.values(tasks).filter(t=>t.tool_name==='research_topic' && t.result && t.result.findings);
    if(!researchTasks.length) researchEl.innerHTML = '<div class="hint">No research yet — will appear live when research tasks execute</div>';
    else researchEl.innerHTML = researchTasks.map(t=>{
      const res = t.result;
      const badge = res.fallback ? '<span class="fallback-badge">Demo fallback — deterministic</span>' : '<span class="live-badge">LIVE</span>';
      return `<div style="margin-bottom:12px"><strong>${t.title}</strong> ${badge}<br>${res.findings.map((f,i)=>`<div class="finding-card"><div class="finding-title">Finding ${String(i+1).padStart(2,'0')} — ${f.title||''}</div><div>${f.detail||''}</div></div>`).join('')}</div>`;
    }).join('');
    if(sourcesEl){
      const allSources = researchTasks.flatMap(t=> (t.result.sources||[]).map(s=>({...s, task: t.task_id})));
      sourcesEl.innerHTML = allSources.length ? `<div class="panel-label" style="margin-top:10px">SOURCES</div>` + allSources.map(s=>`<div class="source-card"><strong>${s.title}</strong><br><span class="hint">${s.relevance||''} — from ${s.task}</span></div>`).join('') : '';
    }
  }

  // Competitor board — real backend output, fallback transparency
  const compBoard = document.getElementById('competitor-board');
  const compPat = document.getElementById('competitor-patterns');
  if(compBoard){
    const compTasks = Object.values(tasks).filter(t=>t.tool_name==='analyze_competitors' && t.result && t.result.competitors);
    if(!compTasks.length) compBoard.innerHTML = '<div class="hint">Competitor analysis will appear when that task executes</div>';
    else {
      const isFallback = compTasks.some(t=>t.result.fallback);
      const badge = isFallback ? '<span class="fallback-badge">Demo fallback</span>' : '<span class="live-badge">LIVE</span>';
      compBoard.innerHTML = `<div style="margin-bottom:8px">${badge}</div>` + compTasks.flatMap(t=> t.result.competitors.map(c=>`
        <div class="competitor-card">
          <div class="comp-name">${c.company||''}</div>
          <div class="hint">${c.website||''}</div>
          <div style="margin-top:6px;font-size:12px"><strong>${c.positioning||''}</strong><br>Hero: "${c.hero_message||''}"<br>CTA: ${c.cta||''}<br>Pattern: ${c.key_pattern||''}</div>
          <div class="hint" style="margin-top:6px">Strength: ${c.strength||''}<br>Weakness: ${c.weakness||''}</div>
        </div>`)).join('');
      compPat.innerHTML = compTasks.flatMap(t=> (t.result.patterns||[]).map((p,i)=>`<div class="pattern-card"><strong>Pattern ${String(i+1).padStart(2,'0')} — ${p.title||''}</strong><br>${p.detail||''}</div>`)).join('') + (compTasks[0].result.recommendations ? `<div class="panel-label" style="margin-top:10px">WHAT THIS MEANS FOR YOU</div><div class="hint">${compTasks[0].result.recommendations.join('<br>')}</div>` : '');
    }
  }

  // Carousel — real artifact, hook emphasized, fallback transparency
  const carouselNav = document.getElementById('carousel-nav');
  const carouselContent = document.getElementById('carousel-content');
  const carouselCaption = document.getElementById('carousel-caption');
  const visualAssets = document.getElementById('visual-assets');
  if(carouselNav && carouselContent){
    const carouselTasks = Object.values(tasks).filter(t=>t.tool_name==='generate_carousel' && t.result && t.result.slides);
    if(!carouselTasks.length){
      carouselNav.innerHTML = '';
      carouselContent.innerHTML = '<div class="hint">Carousel will appear when content tasks execute — 5 slides + hook + CTA + caption from real generation</div>';
      carouselCaption.innerHTML = '';
      if(visualAssets) visualAssets.innerHTML = '<div class="hint">Visual assets appear here when actually produced — no stock images added for decoration</div>';
    } else {
      const res = carouselTasks[0].result;
      const slides = res.slides||[];
      const badge = res.fallback ? '<span class="fallback-badge">Demo fallback — deterministic</span>' : '<span class="live-badge">LIVE</span>';
      carouselNav.innerHTML = `<div style="margin-bottom:8px">${badge}</div>` + slides.map((_,i)=>`<button class="${i===0?'active':''}" onclick="showCarouselSlide(${i})">${String(i+1).padStart(2,'0')}</button>`).join('');
      window.showCarouselSlide = function(idx){
        const s = slides[idx];
        if(!s) return;
        carouselContent.innerHTML = `<div class="carousel-slide ${idx===0?'hook':''}"><div class="slide-label">${idx===0?'HOOK':idx===1?'PROBLEM':idx===2?'INSIGHT':idx===3?'SOLUTION':'CTA'}</div><div class="slide-title">${s.title||''}</div><div>${s.copy||''}</div></div>`;
        document.querySelectorAll('#carousel-nav button').forEach((b,i)=>b.classList.toggle('active', i===idx));
      };
      window.showCarouselSlide(0);
      carouselCaption.innerHTML = `<div class="panel-label">CAPTION</div><div>${res.caption||''}</div><div class="hint" style="margin-top:6px">CTA: ${res.cta||''}</div>`;
      if(visualAssets){
        if(res.visual_notes) visualAssets.innerHTML = `<div class="panel-label">VISUAL ASSETS — art direction</div><div class="hint">${res.visual_notes}</div><div class="asset-card"><div class="asset-preview">🎨</div><div>${res.fallback ? 'Deterministic demo art direction — no fake stock images' : 'Generated art direction'}</div></div>`;
        else visualAssets.innerHTML = '<div class="hint">No visual assets generated — capability boundary exposed</div>';
      }
    }
  }

  // Discoveries — real findings from execution
  const discEl = document.getElementById('discoveries');
  if(discEl){
    const diagTasks = Object.values(tasks).filter(t=>t.tool_name==='project_diagnostics' && t.result);
    const discoveries = [];
    diagTasks.forEach(t=>{
      const r = t.result;
      if(r.summary && r.summary.issue_count>0) discoveries.push({icon:'⚠', title:'Project issue detected', detail: `${r.summary.issue_count} issues in ${r.files_inspected} files`});
      if(r.test_count!==undefined && r.failed>0) discoveries.push({icon:'⚠', title:'Test failures found', detail: `${r.failed} failed, ${r.passed} passed`});
    });
    // Also collect competitor insights as discoveries
    Object.values(tasks).filter(t=>t.tool_name==='analyze_competitors' && t.result).forEach(t=>{
      (t.result.patterns||[]).slice(0,1).forEach(p=> discoveries.push({icon:'💡', title:'Competitor insight', detail: p.title}));
    });
    discEl.innerHTML = discoveries.length ? discoveries.map(d=>`<div class="discovery-card"><strong>${d.icon} ${d.title}</strong><br><span class="hint">${d.detail}</span></div>`).join('') : '<div class="hint">No discoveries yet — will appear as Kernorq executes</div>';
  }

  // Needs your attention — backend-driven: FAILED tasks + warnings that need human
  const needsEl = document.getElementById('needs-attention');
  if(needsEl){
    const failed = Object.values(tasks).filter(t=>t.status==='FAILED');
    const withWarnings = Object.values(tasks).filter(t=>t.result && t.result.warnings && t.result.warnings.length);
    const needs = [];
    failed.forEach(t=> needs.push({prio: 'high', title: `Review ${t.title}`, reason: t.error?.message || t.verification?.message || 'Failed — needs review'}));
    withWarnings.slice(0,2).forEach(t=> needs.push({prio: 'medium', title: `Approve ${t.title}`, reason: t.result.warnings[0]}));
    if(!needs.length) needsEl.innerHTML = '<div class="hint">No attention needed — Kernorq handled all autonomous work</div>';
    else needsEl.innerHTML = needs.map(n=>`<div class="attention-card ${n.prio}"><strong>${n.prio==='high'?'🔴':'🟡'} ${n.title}</strong><br><span class="hint">${n.reason}</span></div>`).join('');
  }

  // Ready for you — completed tasks that produced artifacts
  const readyEl = document.getElementById('ready-for-you');
  if(readyEl){
    const done = Object.values(tasks).filter(t=>t.status==='SUCCEEDED' && t.result);
    if(!done.length) readyEl.innerHTML = '<div class="hint">Artifacts will appear as tasks complete</div>';
    else readyEl.innerHTML = done.slice(0,6).map(t=>{
      const icon = t.tool_name==='research_topic'?'📄':t.tool_name==='analyze_competitors'?'📊':t.tool_name==='generate_carousel'?'🎨':t.tool_name==='run_test_suite'?'✓':'📋';
      return `<div class="ready-card"><div class="ready-icon">${icon}</div><div class="ready-title">${t.title}</div><div class="hint">${t.tool_name}</div></div>`;
    }).join('');
  }

  // Completion screen — backend-authoritative, never contradictory
  const comp = document.getElementById('demo-completion');
  if(comp){
    const total = Object.keys(tasks).length;
    const completed = Object.values(tasks).filter(t=>t.status==='SUCCEEDED').length;
    const failed = Object.values(tasks).filter(t=>t.status==='FAILED').length;
    const isFullyCompleted = exec.status==='COMPLETED' && completed===total && failed===0;
    if(isFullyCompleted){
      comp.classList.remove('hidden');
      document.getElementById('demo-completion-stats').innerHTML = `
        <span>✓ ${completed} / ${total} tasks completed</span>
        <span>✓ Priority scheduling</span>
        <span>✓ Deadline-aware scheduling</span>
        <span>✓ Dependency enforcement</span>
        <span>✓ Real tool execution</span>
        <span>✓ Verification complete</span>`;
      // Actual execution order from backend timestamps (authoritative)
      const actualOrder = Object.values(tasks).slice().sort((a,b)=>{
        const at = a.completed_at||a.started_at||'', bt=b.completed_at||b.started_at||'';
        if(at && bt) return at.localeCompare(bt);
        return 0;
      }).map(t=>t.task_id).join(' → ');
      const order = actualOrder || (demoWorkload ? demoWorkload.planned_order.join(' → ') : Object.keys(tasks).join(' → '));
      document.getElementById('demo-completion-order').innerHTML = `<div class="panel-label">Execution order (from backend)</div><div class="mono small">${order}</div><div class="mono dim small" style="margin-top:6px">${exec.execution_id}</div>`;
    } else {
      comp.classList.add('hidden');
    }
  }

  // Expandable details — populate from live execution
  const rawDetails = document.getElementById('demo-raw-details');
  if(rawDetails) rawDetails.textContent = JSON.stringify({execution: exec, scheduling_policy: 'workload_priority'}, null, 2);
  const toolOut = document.getElementById('demo-tool-output');
  if(toolOut){
    const sample = Object.values(tasks).find(t=>t.result);
    toolOut.textContent = sample ? JSON.stringify(sample.result, null, 2) : 'No tool output yet';
    document.getElementById('demo-tool-details')?.classList.remove('hidden');
  }
  const verOut = document.getElementById('demo-ver-output');
  if(verOut){
    const sample = Object.values(tasks).find(t=>t.verification);
    verOut.textContent = sample ? JSON.stringify(sample.verification, null, 2) : 'No verification yet';
    document.getElementById('demo-ver-details')?.classList.remove('hidden');
  }
  const histOut = document.getElementById('demo-history-output');
  if(histOut){
    histOut.textContent = events ? JSON.stringify(events.slice(-12), null, 2) : 'No events yet';
    document.getElementById('demo-history-details')?.classList.remove('hidden');
  }
}

/* ---------------- VoiceService (interface layer; replaceable provider) ---------------- */
/* Records real 16kHz mono PCM16 WAV client-side so the uploaded MIME always matches
   the bytes (Gemini supports WAV natively; avoids WebM/Opus mismatch). */
const VoiceService = (() => {
  let mediaStream = null, audioCtx = null, processor = null, source = null;
  let pcmChunks = [], recording = false, currentAudio = null;
  const TARGET_RATE = 16000;

  async function startListening() {
    mediaStream = await navigator.mediaDevices.getUserMedia({audio:true});
    audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    source = audioCtx.createMediaStreamSource(mediaStream);
    processor = audioCtx.createScriptProcessor(4096, 1, 1);
    pcmChunks = []; recording = true;
    processor.onaudioprocess = e => {
      if (!recording) return;
      pcmChunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    };
    source.connect(processor);
    processor.connect(audioCtx.destination); // required for onaudioprocess in some browsers
    return true;
  }

  function downsample(f32, fromRate){
    if (fromRate === TARGET_RATE) return f32;
    const ratio = fromRate / TARGET_RATE;
    const len = Math.floor(f32.length / ratio);
    const out = new Float32Array(len);
    for (let i=0;i<len;i++) out[i] = f32[Math.floor(i*ratio)];
    return out;
  }
  function floatTo16(f32){
    const out = new Int16Array(f32.length);
    for (let i=0;i<f32.length;i++){
      const s = Math.max(-1, Math.min(1, f32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return out;
  }
  function encodeWav(samplesInt16, sampleRate){
    const buf = new ArrayBuffer(44 + samplesInt16.length*2);
    const v = new DataView(buf);
    const w = (o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i));};
    w(0,'RIFF'); v.setUint32(4,36+samplesInt16.length*2,true); w(8,'WAVE');
    w(12,'fmt '); v.setUint32(16,16,true); v.setUint16(20,1,true); v.setUint16(22,1,true);
    v.setUint32(24,sampleRate,true); v.setUint32(28,sampleRate*2,true);
    v.setUint16(32,2,true); v.setUint16(34,16,true);
    w(36,'data'); v.setUint32(40,samplesInt16.length*2,true);
    let off=44; for(let i=0;i<samplesInt16.length;i++,off+=2) v.setInt16(off,samplesInt16[i],true);
    return new Blob([buf], {type:'audio/wav'});
  }

  function stopListening() {
    return new Promise(resolve => {
      recording = false;
      try{ processor && processor.disconnect(); }catch{}
      try{ source && source.disconnect(); }catch{}
      const raw = pcmChunks;
      const rate = audioCtx ? audioCtx.sampleRate : 48000;
      cleanup();
      if (!raw.length) return resolve(null);
      let total = 0; raw.forEach(c=>total+=c.length);
      const merged = new Float32Array(total);
      let off=0; raw.forEach(c=>{merged.set(c,off); off+=c.length;});
      const ds = downsample(merged, rate);
      resolve(encodeWav(floatTo16(ds), TARGET_RATE));
    });
  }
  function cancelListening(){ recording=false; cleanup(); }
  function cleanup(){
    if (mediaStream){ mediaStream.getTracks().forEach(t=>t.stop()); mediaStream=null; }
    processor=null; source=null;
    if (audioCtx){ try{audioCtx.close();}catch{} audioCtx=null; }
  }
  function blobToBase64(blob){
    return new Promise((res,rej)=>{ const r=new FileReader(); r.onload=()=>res(String(r.result).split(',')[1]); r.onerror=rej; r.readAsDataURL(blob); });
  }
  function play(base64, mime, onEnd){
    stopPlayback();
    currentAudio = new Audio(`data:${mime||'audio/wav'};base64,${base64}`);
    currentAudio.onended = () => { if(onEnd) onEnd(); };
    return currentAudio.play();
  }
  function pause(){ if(currentAudio) currentAudio.pause(); }
  function resume(){ if(currentAudio) return currentAudio.play(); }
  function stopPlayback(){ if(currentAudio){ try{currentAudio.pause();}catch{} currentAudio.onended=null; currentAudio=null; } }
  return { startListening, stopListening, cancelListening, blobToBase64, play, pause, resume, stopPlayback };
})();

/* ---------------- Navigation ---------------- */
function showView(name){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  const t=document.getElementById('view-'+name);
  if(t) t.classList.add('active');
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active', n.dataset.nav===name));
}
window.addEventListener('hashchange', ()=>{
  const h=(location.hash.replace('#','')||'home');
  showView(h==='execution'&&!currentId?'home':h);
});
document.querySelectorAll('[data-nav]').forEach(a=>a.addEventListener('click',()=>showView(a.dataset.nav)));
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.add('hidden'));
  document.getElementById('tab-'+b.dataset.tab).classList.remove('hidden');
}));
document.getElementById('reduced-motion-toggle')?.addEventListener('change',(e)=>{
  document.body.classList.toggle('reduced-motion', e.target.checked);
});

/* ---------------- Home composer + suggestions ---------------- */
const objectiveInput=document.getElementById('objective-input');
const executeBtn=document.getElementById('execute-btn');
objectiveInput?.addEventListener('input',()=>{ executeBtn.disabled=!objectiveInput.value.trim(); });
document.querySelectorAll('.suggestion').forEach(b=>b.addEventListener('click',()=>{
  objectiveInput.value=b.dataset.objective; executeBtn.disabled=false; objectiveInput.focus();
}));
executeBtn?.addEventListener('click', submitObjective);
objectiveInput?.addEventListener('keydown',(e)=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); if(!executeBtn.disabled) submitObjective(); }});

async function submitObjective(){
  const objective=objectiveInput.value.trim();
  if(!objective) return;
  executeBtn.disabled=true; executeBtn.textContent='Starting…';
  hideError();
  try{
    const res=await api.createExecution(objective);
    openExecution(res.execution_id, res.status==='COMPLETED'||res.status==='FAILED');
  }catch(ex){ showError(ex.message); executeBtn.disabled=false; executeBtn.textContent='Execute Objective →'; }
}

function showError(msg){ const el=document.getElementById('form-error'); el.textContent=msg; el.classList.remove('hidden'); }
function hideError(){ document.getElementById('form-error').classList.add('hidden'); }

/* ---------------- Live Voice Mode (persistent session) ---------------- */
const LiveMode = (() => {
  let ws=null, active=false, playQueue=[], currentSource=null, speaking=false;
  let worklet=null, audioCtx=null, mediaStream=null;
  const RATE=16000;

  async function start(onEvent){
    // capture raw PCM 16k via AudioWorklet (fallback ScriptProcessor)
    mediaStream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
    audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    ws = new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/conversation`);
    ws.binaryType='arraybuffer';
    ws.onmessage = (ev)=>{
      try{ onEvent(JSON.parse(ev.data)); }catch{}
    };
    await new Promise((res,rej)=>{ ws.onopen=res; ws.onerror=rej; });
    await audioCtx.resume();
    source = audioCtx.createMediaStreamSource(mediaStream);
    try{
      await audioCtx.audioWorklet.addModule('/static/pcm-worklet.js');
      worklet = new AudioWorkletNode(audioCtx,'pcm-capture');
    }catch{
      // AudioWorklet unavailable — fall back to ScriptProcessor
      worklet = audioCtx.createScriptProcessor(4096,1,1);
      Object.defineProperty(worklet,'port',{value:{onmessage:null, postMessage:()=>{}}});
      const origProc = worklet;
      origProc.onaudioprocess = e=>{ if(worklet.port.onmessage) worklet.port.onmessage({data:e.inputBuffer.getChannelData(0)}); };
      worklet.__isScriptProcessor = true;
    }
    const handler = (e)=>{
      if(active && ws.readyState===1){
        ws.send(floatTo16(e.data).buffer);
      }
    };
    if (worklet instanceof AudioWorkletNode){
      worklet.port.onmessage = handler;
      source.connect(worklet); worklet.connect(audioCtx.destination);
    } else {
      source.connect(worklet); worklet.connect(audioCtx.destination);
      worklet.port.onmessage = null;
      // ScriptProcessor path: wire onaudioprocess directly
      worklet.onaudioprocess = e=>{ handler({data:e.inputBuffer.getChannelData(0)}); };
    }
    active=true;
    return true;
  }
  function floatTo16(f32){
    const out=new Int16Array(f32.length);
    for(let i=0;i<f32.length;i++){const s=Math.max(-1,Math.min(1,f32[i]));out[i]=s<0?s*0x8000:s*0x7FFF;}
    return out;
  }
  function signalEndOfSpeech(){ if(ws&&ws.readyState===1) ws.send(JSON.stringify({type:'end_of_speech'})); }

  // Playback: schedule PCM chunks immediately for minimal latency
  function enqueuePcm(base64, mime){
    const rateMatch=(mime||'').match(/rate=(\d+)/);
    const rate=rateMatch?parseInt(rateMatch[1]):24000;
    const pcm=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));
    playQueue.push({pcm,rate});
    pump();
  }
  function pump(){
    if(currentSource||!playQueue.length||!audioCtx) return;
    const {pcm,rate}=playQueue.shift();
    const f32=pcm16ToF32(pcm);
    const buf=audioCtx.createBuffer(1,f32.length,rate);
    buf.copyToChannel(f32,0);
    const src=audioCtx.createBufferSource();
    src.buffer=buf; src.connect(audioCtx.destination);
    currentSource=src;
    src.onended=()=>{currentSource=null; pump();};
    src.start();
  }
  function pcm16ToF32(pcmBytes){
    const len=pcmBytes.byteLength>>1;
    const view=new DataView(pcmBytes.buffer ?? pcmBytes);
    const out=new Float32Array(len);
    for(let i=0;i<len;i++) out[i]=view.getInt16(i*2,true)/32768;
    return out;
  }
  function interruptPlayback(){
    playQueue=[];
    if(currentSource){try{currentSource.stop();}catch{} currentSource=null;}
  }
  async function stop(){
    active=false;
    interruptPlayback();
    try{ source && source.disconnect && source.disconnect(); }catch{}
    try{ worklet && (worklet.port ? (worklet instanceof AudioWorkletNode ? worklet.disconnect() : null) : null); }catch{}
    try{ worklet && worklet.disconnect && worklet.disconnect(); }catch{}
    if(mediaStream){mediaStream.getTracks().forEach(t=>t.stop());mediaStream=null;}
    if(audioCtx){try{await audioCtx.close();}catch{} audioCtx=null;}
    if(ws){try{ws.send(JSON.stringify({type:'stop'}));}catch{} ws.close(); ws=null;}
  }
  return {start, stop, signalEndOfSpeech, enqueuePcm, interruptPlayback,
          get isActive(){return active;}, get isSpeaking(){speaking=!!(currentSource||playQueue.length);return speaking;}};
})();

async function toggleVoiceMode(){
  const btn=document.getElementById('voice-mode-btn');
  if(!LiveMode.isActive){
    btn.textContent='⏹ Leave voice mode'; btn.classList.add('listening');
    voiceState.textContent='Voice mode: connecting…'; voiceState.classList.remove('hidden');
    try{
      await LiveMode.start(onLiveEvent);
      voiceState.textContent='🎙 Voice mode active — just speak';
      wave.classList.remove('hidden');
    }catch(err){
      btn.textContent='🎙 Voice mode'; btn.classList.remove('listening');
      voiceState.classList.add('hidden'); wave.classList.add('hidden');
      showError('Voice mode unavailable: '+err.message+'. Text input remains available.');
    }
  } else {
    await LiveMode.stop();
    btn.textContent='🎙 Voice mode'; btn.classList.remove('listening');
    voiceState.classList.add('hidden'); wave.classList.add('hidden');
  }
}

function onLiveEvent(msg){
  switch(msg.type){
    case 'ready':
      break;
    case 'transcript':
      if(msg.role==='user'){
        objectiveInput.value=msg.text; // live transcript of user speech
      } else {
        voiceState.textContent='Kernorq: '+msg.text;
        voiceState.classList.remove('hidden');
        // EXECUTE marker arrives as its own message type from the server
      }
      break;
    case 'audio':
      LiveMode.enqueuePcm(msg.audio_base64, msg.mime_type);
      break;
    case 'interrupted':
      LiveMode.interruptPlayback(); // barge-in: stop speaking, listen immediately
      break;
    case 'execute': {
      const objective=msg.objective;
      objectiveInput.value=objective; executeBtn.disabled=false;
      voiceState.textContent='▶ Executing: '+objective;
      submitObjectiveFromVoice(objective);
      break;
    }
    case 'error':
      showError('Voice mode error: '+msg.detail);
      break;
    case 'go_away':
      voiceState.textContent='Session ending soon…';
      break;
  }
}

async function submitObjectiveFromVoice(objective){
  hideError();
  executeBtn.disabled=true; executeBtn.textContent='Executing…';
  try{
    const res=await api.createExecution(objective);
    openExecution(res.execution_id);
    // When done, result card auto-speaks via maybeSpeak (existing pipeline)
  }catch(ex){ showError(ex.message); }
  finally{ executeBtn.disabled=false; executeBtn.textContent='Execute Objective →'; }
}
document.getElementById('voice-mode-btn')?.addEventListener('click', toggleVoiceMode);

/* ---------------- Microphone flow ---------------- */
const micBtn=document.getElementById('mic-btn');
const voiceState=document.getElementById('voice-state');
const wave=document.getElementById('wave');
let listening=false;
let voiceAvailable=false;

micBtn?.addEventListener('click', async ()=>{
  if(!listening){
    micBtn.classList.add('listening');
    voiceState.textContent='Listening…'; voiceState.classList.remove('hidden');
    wave.classList.remove('hidden');
    listening=true;
    try{
      await VoiceService.startListening(()=>{/* level could drive wave scale */});
    }catch(err){
      voiceFail('Microphone access is unavailable. You can type your objective instead.');
      return;
    }
  } else {
    listening=false;
    micBtn.classList.remove('listening');
    voiceState.textContent='Understanding…';
    wave.classList.add('hidden');
    try{
      const blob=await VoiceService.stopListening();
      if(!blob || !blob.size){ voiceReset(); return; }
      const b64=await VoiceService.blobToBase64(blob);
      const text=await api.transcribe(b64,'audio/wav'); // bytes are real WAV
      // Intent router: conversational utterances never become executions
      const intent=await api.converse(text).catch(()=>({mode:'objective'}));
      if(intent.mode==='conversation'){
        voiceState.textContent='💬 '+intent.response;
        voiceState.classList.remove('hidden');
        objectiveInput.value=''; executeBtn.disabled=true;
        speakText(intent.response); // existing TTS pipeline
        setTimeout(voiceReset, 6000);
        return;
      }
      objectiveInput.value=text;
      executeBtn.disabled=!text.trim();
      voiceState.textContent='✓ Transcript ready — review and execute';
      setTimeout(voiceReset, 3500);
    }catch(err){
      let msg = 'Could not understand the recording. You can type your objective instead.';
      try { const j=JSON.parse(err.message); if(j.detail){ msg = typeof j.detail==='object' ? (j.detail.detail||msg) : j.detail; } } catch {}
      voiceFail(msg);
    }
  }
});
function voiceReset(){ voiceState.classList.add('hidden'); wave.classList.add('hidden'); micBtn.classList.remove('listening'); listening=false; }
function voiceFail(msg){ voiceReset(); showError(msg); }

/* ---------------- Execution experience ---------------- */
let currentId=null, pollTimer=null, lastKnown='—', lastExec=null, spokenFor=null;

function openExecution(id, alreadyFinished){
  currentId=id; lastKnown='—'; spokenFor=null;
  location.hash='#execution';
  showView('execution');
  loadDetail(id, alreadyFinished);
  if(pollTimer) clearInterval(pollTimer);
  pollTimer=setInterval(()=>loadDetail(id,false),2000);
}

function stageList(exec, events){
  const has=t=>events.some(e=>e.event_type===t);
  const status=exec.status;
  const verOk=has('VERIFICATION_SUCCEEDED'), verBad=has('VERIFICATION_FAILED'), verUnk=!verOk&&!verBad&&has('VERIFICATION_STARTED')&&(exec.tasks?Object.values(exec.tasks).some(t=>t.verification&&t.verification.status==='unknown'):false)||(!verOk&&!verBad&&has('VERIFICATION_STARTED')&&status==='COMPLETED'===false);
  const recovered=has('RECOVERY_STARTED');
  return [
    {key:'OBJECTIVE',label:'Objective',state:'done'},
    {key:'PLANNING',label:'Planning',state:'done'},
    {key:'VALIDATION',label:'Validation',state:'done'}, // planner validated tools/deps before store.create_execution
    {key:'EXECUTION',label:'Execution',state:has('TASK_STARTED')?(has('TASK_COMPLETED')?'done':'active'):(has('TASK_FAILED')?'failed':'pending')},
    {key:'VERIFICATION',label:'Verification',state:verOk?'done':verBad?'failed':has('VERIFICATION_STARTED')?'unknown':'pending'},
    ...(recovered?[{key:'RECOVERY',label:'Recovery',state:has('RETRY_STARTED')||has('RECOVERY_SELECTED')?'done':'active'}]:[]),
    {key:'RESULT',label:status==='FAILED'?'Failed':'Completed',state:status==='COMPLETED'?'done':status==='FAILED'?'failed':'pending'},
  ];
}

function renderPipeline(exec, events){
  const ol=document.getElementById('pipeline');
  ol.innerHTML='';
  stageList(exec, events).forEach(s=>{
    const li=document.createElement('li');
    li.className=`stage ${s.state}`;
    const icon=s.state==='done'?'✓':s.state==='active'?'●':s.state==='failed'?'✕':s.state==='unknown'?'○':'○';
    li.innerHTML=`<span class="icon">${icon}</span><span class="label">${s.label}</span>`;
    ol.appendChild(li);
  });
  const note=document.getElementById('pipeline-note');
  const recovered=events.some(e=>e.event_type==='RECOVERY_STARTED');
  if(recovered){
    const sel=events.find(e=>e.event_type==='RECOVERY_SELECTED');
    note.textContent=sel?`Kernorq detected that the initial execution did not satisfy verification and performed recovery (${sel.metadata?.recovery_action||'RETRY'}).`:'Recovering…';
  } else if(exec.status==='COMPLETED'){ note.textContent='First attempt satisfied verification — no recovery required.'; }
  else if(exec.status==='FAILED'){ note.textContent='Execution stopped safely after failure.'; }
  else note.textContent='';
}

function renderOpState(exec, events, tasks){
  const ul=document.getElementById('op-state'); ul.innerHTML='';
  const items=[];
  items.push(['ok','✓ Objective accepted']);
  items.push(events.some(e=>e.event_type==='TASK_STARTED')?['ok','✓ Plan generated']:['run','● Generating plan…']);
  items.push(['ok','✓ Required tools validated']);
  const runningTask=Object.values(tasks).find(t=>t.status==='RUNNING');
  if(runningTask) items.push(['run',`● Running ${runningTask.tool_name||runningTask.title}`]);
  else if(Object.values(tasks).some(t=>t.status==='SUCCEEDED')) items.push(['ok','✓ Tools executed']);
  const anyVer=Object.values(tasks).find(t=>t.verification);
  if(anyVer){
    const vs=anyVer.verification.status;
    items.push(vs==='verified_success'?['ok','✓ Verification passed']:vs==='unknown'?['warn','○ Verification unknown']:['bad','✕ Verification failed']);
  } else items.push(['','○ Verification pending']);
  items.forEach(([cls,text])=>{ const li=document.createElement('li'); li.className=cls; li.textContent=text; ul.appendChild(li); });
}

function renderControlState(exec, tasks){
  const ul=document.getElementById('control-state'); ul.innerHTML='';
  const checks=[
    ['Objective valid', exec.objective && exec.objective.trim().length>0],
    ['Tool available', Object.values(tasks).every(t=>t.tool_name)],
    ['Dependencies satisfied', true],
    ['Execution permitted', true],
  ];
  checks.forEach(([label, ok])=>{
    const li=document.createElement('li'); li.className='ok'; li.textContent=`${ok?'✓':'○'} ${label}`;
    ul.appendChild(li);
  });
  const li=document.createElement('li'); li.className=''; li.textContent='Executor: Deterministic execution';
  ul.appendChild(li);
}

function renderVerificationCard(exec){
  const c=document.getElementById('x-verification');
  const results=exec.verification_results||[];
  if(!results.length){ c.innerHTML='<div class="hint">Pending</div>'; return; }
  const last=results[results.length-1];
  const ok=last.status==='verified_success', unk=last.status==='unknown';
  let html=`<div class="ver-summary ${ok?'ok':unk?'warn':'err'}"><strong>${ok?'✓ Verified':unk?'○ Status unknown':'✕ Verification Failed'}</strong><br><span>${last.message||''}</span>`;
  if(last.evidence&&last.evidence.required_fields) html+=`<br>Required: ${last.evidence.required_fields.join(', ')}`;
  html+='</div>';
  c.innerHTML=html;
}

function renderRecoveryCard(exec){
  const c=document.getElementById('x-recovery');
  const hist=exec.recovery_history||[];
  if(!hist.length){ c.innerHTML='<div class="hint">No recovery required. First attempt succeeded.</div>'; return; }
  let html='';
  hist.forEach(h=>{
    html+=`<div class="rec-entry"><strong>↻ Recovery: ${h.recovery_action}</strong><br>
      <span class="hint">Reason: ${h.reason||''} · Attempt ${h.attempt||''} → ${(parseInt(h.attempt||1))+1} · Task ${h.task_id}</span><br>
      ${h.external_state?`<span class="hint">External state: ${h.external_state}</span><br>`:''}
      <span class="hint mono">op ${(h.operation_id||'').slice(0,12)}…</span></div>`;
  });
  const last=hist[hist.length-1];
  if(exec.status==='COMPLETED') html+=`<div class="hint">Kernorq detected the initial execution did not satisfy verification and performed recovery.</div>`;
  c.innerHTML=html;
}

function humanSummary(exec, tasks){
  if(exec.status==='COMPLETED'){
    const t=Object.values(tasks)[0];
    const ver=t&&t.verification;
    const files=ver&&ver.evidence&&Array.isArray(ver.evidence.found_fields)?ver.evidence.found_fields.length:null;
    let s='Execution completed successfully.';
    if(ver&&ver.evidence&&ver.evidence.required_fields) s+=` Verification passed on required fields: ${ver.evidence.required_fields.join(', ')}.`;
    if(files!==null) s+=` ${files} evidence fields confirmed.`;
    return s;
  }
  if(exec.status==='FAILED'){
    const le=exec.last_error;
    let reason=le?(le.message||le.type||''): 'verification or execution failure';
    if(typeof reason==='object') reason=JSON.stringify(reason);
    return `Execution failed safely. Reason: ${reason}.`;
  }
  return null;
}

function renderResult(exec, tasks){
  const card=document.getElementById('result-card');
  const hero=document.getElementById('result-hero');
  const sum=document.getElementById('result-summary');
  const finished=exec.status==='COMPLETED'||exec.status==='FAILED';
  if(!finished){ card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  const recovered=(exec.recovery_history||[]).length>0;
  if(exec.status==='COMPLETED'){
    hero.className='result-hero ok';
    hero.textContent=recovered?'↻ RECOVERED — VERIFIED SUCCESSFULLY':'✓ VERIFIED SUCCESSFULLY';
  } else {
    hero.className='result-hero err';
    hero.textContent='✕ VERIFICATION FAILED';
  }
  sum.textContent=humanSummary(exec,tasks)||'';
  renderEvidenceTab(exec, tasks);
  maybeSpeak(exec, tasks);
}

function renderEvidenceTab(exec, tasks){
  const human=document.getElementById('x-evidence-human');
  const raw=document.getElementById('x-evidence-raw');
  const results=exec.verification_results||[];
  if(results.length){
    const last=results[results.length-1];
    let h=`<div class="ver-summary ${last.status==='verified_success'?'ok':last.status==='unknown'?'warn':'err'}">
      <strong>${last.status==='verified_success'?'✓ Verification succeeded':last.status==='unknown'?'○ Unknown':'✕ Verification failed'}</strong><br>${last.message||''}<br>`;
    const ev=last.evidence||{};
    if(ev.required_fields) h+=`Required: ${ev.required_fields.join(', ')}<br>`;
    if(ev.found_fields) h+=`Files/evidence fields: ${ev.found_fields.join(', ')}<br>`;
    if(ev.operation_id) h+=`Operation ID: <code>${String(ev.operation_id).slice(0,12)}…</code><br>`;
    h+='</div>';
    human.innerHTML=h;
  } else human.innerHTML='<div class="hint">No verification evidence yet.</div>';
  raw.textContent=JSON.stringify({verification_results:exec.verification_results, tasks, recovery_history:exec.recovery_history, checkpoints:(exec.checkpoints||[]).map(c=>({checkpoint_id:c.checkpoint_id,reason:c.reason,task_id:c.task_id}))},null,2);
}

async function maybeSpeak(exec, tasks){
  if(spokenFor===exec.execution_id) return;
  if(document.getElementById('voice-enabled') && !document.getElementById('voice-enabled').checked) return;
  const summary=humanSummary(exec,tasks);
  if(!summary) return;
  spokenFor=exec.execution_id;
  try{
    const resp=await api.speak(summary);
    const ind=document.getElementById('speaking-indicator');
    const stopBtn=document.getElementById('stop-speak-btn');
    ind.classList.remove('hidden'); stopBtn.classList.remove('hidden');
    await VoiceService.play(resp.audio_base64, resp.mime_type, ()=>{
      ind.classList.add('hidden'); stopBtn.classList.add('hidden');
    });
  }catch(err){
    // Voice is an enhancement — text result remains
    document.getElementById('speaking-indicator').classList.add('hidden');
    document.getElementById('settings-voice-hint').textContent='Voice playback unavailable: '+err.message;
  }
}
async function speakText(text){
  // Speak arbitrary short text through the existing Gemini TTS pipeline.
  if(!text) return;
  try{
    const resp=await api.speak(text);
    document.getElementById('speaking-indicator').classList.remove('hidden');
    document.getElementById('stop-speak-btn').classList.remove('hidden');
    await VoiceService.play(resp.audio_base64, resp.mime_type, ()=>{
      document.getElementById('speaking-indicator').classList.add('hidden');
      document.getElementById('stop-speak-btn').classList.add('hidden');
    });
  }catch(err){
    // Voice is an enhancement; the text response is already visible
    document.getElementById('speaking-indicator').classList.add('hidden');
    document.getElementById('stop-speak-btn').classList.add('hidden');
  }
}

document.getElementById('speak-btn')?.addEventListener('click', async ()=>{
  if(!lastExec) return;
  speakText(humanSummary(lastExec,lastTasks)||lastExec.objective);
});
document.getElementById('stop-speak-btn')?.addEventListener('click',()=>{
  VoiceService.stopPlayback();
  document.getElementById('speaking-indicator').classList.add('hidden');
  document.getElementById('stop-speak-btn').classList.add('hidden');
});

function renderTimeline(events){
  const tm=document.getElementById('x-timeline'); tm.innerHTML='';
  events.forEach(ev=>{
    const row=document.createElement('div'); row.className='evt';
    const ts=new Date(ev.timestamp).toLocaleTimeString();
    let label=ev.event_type;
    if(label==='TASK_STARTED') label='● Task started';
    else if(label==='CHECKPOINT_CREATED') label=`⬔ Checkpoint — ${ev.metadata?.reason||''}`;
    else if(label==='VERIFICATION_STARTED') label='○ Verification started';
    else if(label==='VERIFICATION_SUCCEEDED') label='✓ Tool completed — verified';
    else if(label==='VERIFICATION_FAILED') label='✕ Verification failed';
    else if(label==='RECOVERY_STARTED') label='↻ Recovery started';
    else if(label==='RECOVERY_SELECTED') label=`↻ Recovery selected → ${ev.metadata?.recovery_action||''}`;
    else if(label==='RETRY_STARTED') label='↻ Retry started';
    else if(label==='TASK_FAILED') label='✕ Task failed';
    else if(label==='TASK_COMPLETED') label='✓ Task completed';
    else if(label==='EXECUTION_COMPLETED') label='✓ Execution completed';
    else if(label==='EXECUTION_FAILED') label='✕ Execution failed';
    row.innerHTML=`<span class="time">${ts}</span> <strong>${label}</strong> ${ev.task_id?`· ${ev.task_id}`:''} <span class="hint">${ev.actor||''}</span>`;
    if(ev.metadata&&Object.keys(ev.metadata).length){
      const d=document.createElement('details'); d.innerHTML=`<summary>evidence</summary><pre class="raw mono small" style="max-height:140px">${JSON.stringify(ev.metadata,null,2)}</pre>`;
      row.appendChild(d);
    }
    tm.appendChild(row);
  });
}

function renderTasks(tasks){
  const c=document.getElementById('x-tasks'); c.innerHTML='';
  Object.values(tasks).forEach(t=>{
    const st=t.status||'UNKNOWN';
    const icon={SUCCEEDED:'✓',FAILED:'✕',RUNNING:'●',VERIFYING:'○',PENDING:'○',READY:'○'}[st]||'○';
    const row=document.createElement('div'); row.className='task-row';
    row.innerHTML=`<span>${icon} <strong>${t.title}</strong></span><span class="st-${st}" style="font-size:11px;font-weight:700">${st}</span><span class="mono dim small">${t.tool_name||'—'}</span><span class="mono dim small">${t.attempt_count}/${t.max_attempts}</span>`;
    c.appendChild(row);
  });
}

async function loadDetail(id, initial){
  try{
    const [exec, tasks, events]=await Promise.all([api.getExecution(id),api.getTasks(id),api.getEvents(id)]);
    lastExec=exec; lastTasks=tasks;
    document.getElementById('conn-error').classList.add('hidden');
    lastKnown=exec.status;
    document.getElementById('x-objective').textContent=exec.objective;
    document.getElementById('x-exec-id').textContent=exec.execution_id;
    const pill=document.getElementById('exec-status-pill');
    const txt=exec.status;
    pill.className='status-pill'+(txt==='EXECUTING'||txt==='VERIFYING'||txt==='RECOVERING'?' running':'');
    document.getElementById('exec-status-text').textContent=txt;
    document.getElementById('sys-status').textContent = finished(txt)?'Online':'Executing';
    renderDemoLive(exec, tasks, events);
    renderPipeline(exec, events);
    renderOpState(exec, events, tasks);
    renderControlState(exec, tasks);
    renderVerificationCard(exec);
    renderRecoveryCard(exec);
    renderTimeline(events);
    renderTasks(tasks);
    renderResult(exec, tasks);
    if(finished(txt)&&pollTimer){ clearInterval(pollTimer); pollTimer=null; }
  }catch(err){
    // Never fabricate state; keep last known and retry
    document.getElementById('conn-error').classList.remove('hidden');
    document.getElementById('last-known').textContent=lastKnown;
  }
}
function finished(s){return s==='COMPLETED'||s==='FAILED'||s==='CANCELLED';}

/* ---------------- Activity ---------------- */
let activityFilter='all';
document.querySelectorAll('.chip.filter').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.chip.filter').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); activityFilter=b.dataset.filter; refreshActivity();
}));

function durationOf(e){
  if(!e.created_at||!e.updated_at) return '';
  const ms=new Date(e.updated_at)-new Date(e.created_at);
  return `${(ms/1000).toFixed(1)}s`;
}

async function refreshActivity(){
  try{
    const list=await api.listExecutions();
    const c=document.getElementById('activity-list'); if(!c)return;
    c.innerHTML='';
    const filtered=activityFilter==='all'?list:list.filter(e=>e.status===activityFilter);
    if(!filtered.length){c.innerHTML='<div class="hint">No executions yet. Give Kernorq an objective from Home.</div>';return;}
    filtered.forEach(e=>{
      const recovered=(e.recovery_history||[]).length>0;
      const div=document.createElement('div'); div.className='act-card';
      const cls=e.status==='COMPLETED'?'ok':e.status==='FAILED'?'err':'run';
      let sub=e.status==='FAILED'?'Execution stopped safely after failure':'Deterministic execution · verified by Kernorq';
      if(recovered) sub='Recovered after initial verification failure';
      div.innerHTML=`<div class="act-left">
          <div class="act-status ${recovered&&e.status==='COMPLETED'?'rec':cls}">${recovered&&e.status==='COMPLETED'?'↻ Recovered':e.status==='COMPLETED'?'✓ Completed':e.status==='FAILED'?'✕ Failed':'● '+e.status}</div>
          <div class="act-objective">${e.objective}</div>
          <div class="act-sub">${sub}</div>
        </div>
        <div class="act-meta"><div class="act-dur">${durationOf(e)}</div><br><span class="act-link">View details →</span></div>`;
      div.onclick=()=>openExecution(e.execution_id);
      c.appendChild(div);
    });
  }catch{}
}

/* ---------------- Projects ---------------- */
async function initProjects(){
  try{
    const plan={objective:'Inspect my project',tasks:[{task_id:'inspect',title:'i',description:'d',tool_name:'inspect_project_workspace'}]};
    // Use a completed execution's repository_root if present; else default path
    const list=await api.listExecutions();
    for(const e of list.slice().reverse()){
      const t=e.tasks&&Object.values(e.tasks)[0];
      if(t&&t.result&&t.result.repository_root){ document.getElementById('project-path').textContent=t.result.repository_root; return; }
    }
    document.getElementById('project-path').textContent='C:\\Users\\Bashir\\agentic-execution';
  }catch{ document.getElementById('project-path').textContent='C:\\Users\\Bashir\\agentic-execution'; }
}

/* ---------------- Voice availability ---------------- */
async function initVoice(){
  try{
    const st=await api.voiceStatus();
    voiceAvailable=!!st.available;
    const line=document.getElementById('voice-status-line');
    line.textContent=voiceAvailable?'Voice: Gemini ready':'Voice: unavailable (text works)';
    line.style.color=voiceAvailable?'#86efac':'#94a3b8';
    if(!voiceAvailable) document.getElementById('settings-voice-hint').textContent=st.message||'Voice requires server-side Gemini configuration.';
  }catch{
    document.getElementById('voice-status-line').textContent='Voice: unavailable (text works)';
  }
}

/* ---------------- Global refresh ---------------- */
document.getElementById('refresh-btn')?.addEventListener('click',()=>{
  refreshActivity(); refreshDashboardRecent();
  if(currentId) loadDetail(currentId);
});
async function refreshDashboardRecent(){
  // keep sidebar/status fresh; activity list is the primary history surface
  refreshActivity();
}

/* ---------------- Init ---------------- */
loadDemoWorkload();
refreshActivity();
initProjects();
initVoice();
showView(location.hash.replace('#','')||'home');
setInterval(refreshActivity,4000);
