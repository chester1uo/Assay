// Research workbench: real persisted evidence, no simulated agent actions.
export function render(root, {api, el}) {
  const host = el('div', {className: 'research-workbench'});
  root.replaceChildren(host);
  const style = el('style', {}, `
    body:has(.research-workbench) .contextbar-controls {display:none}
    .research-workbench {max-width:1500px;margin:auto;padding:24px}
    .research-workbench h1 {margin:0;font-size:28px}
    .research-workbench h2 {font-size:17px;margin:0 0 14px}
    .research-workbench .rw-muted {color:var(--text-muted,#758294);font-size:13px}
    .rw-toolbar,.rw-actions {display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:16px 0}
    .rw-grid {display:grid;grid-template-columns:minmax(280px,1fr) minmax(400px,1.8fr);gap:18px;align-items:start}
    .rw-panel {border:1px solid var(--border,#ccd4df);border-radius:12px;padding:20px;min-width:0}
    .research-workbench input,.research-workbench textarea,.research-workbench select {max-width:100%;padding:8px;border:1px solid #99a6b7;border-radius:5px;background:var(--bg,#fff);color:inherit}
    .research-workbench textarea {width:100%;min-height:70px;box-sizing:border-box}
    .research-workbench label {display:block;margin:10px 0;font-size:13px}
    .research-workbench button {cursor:pointer;padding:8px 13px;border-radius:5px;border:1px solid #8999ad;background:var(--bg,#fff);color:inherit}
    .research-workbench button:disabled {opacity:.5;cursor:wait}
    .rw-error {color:#c43f49;white-space:pre-wrap}
    .rw-attempt {border-left:3px solid #6485ba;padding:10px;margin:8px 0;background:var(--bg-subtle,rgba(128,148,175,.08));overflow-wrap:anywhere}
    .rw-attempt.failed {border-color:#d25d61}
    .rw-attempt.cancelled {border-color:#8999ad}
    .rw-attempt.completed {border-color:#39a385}
    .rw-attempt.running {border-color:#dfab41}
    .rw-attempt code {display:block;margin:8px 0;white-space:pre-wrap}
    .rw-detail {white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:350px;overflow:auto}
    .rw-lineage {display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
    .rw-lineage button {font-family:monospace;font-size:12px}
    @media(max-width:850px){.rw-grid{grid-template-columns:1fr}}
  `);
  let sid = '', state = null, selected = null, busy = false, revision = 0, selection = 0, listRequest = 0, retry = null;
  const status = el('p', {role:'status', 'aria-live':'polite'});
  const error = el('p', {className:'rw-error', role:'alert'});
  const sessions = el('select', {'aria-label':'Research session'});
  const datasets = el('select', {'aria-label':'Dataset'});
  const goal = el('textarea', {'aria-label':'Research goal', placeholder:'Describe the hypothesis and research objective'});
  const budget = el('input', {type:'number', min:1, value:6, 'aria-label':'Experiment budget'});
  const objective = el('div');
  const attempts = el('div');
  const lineage = el('div');
  const detail = el('pre', {className:'rw-detail'});
  const selectedSummary = el('p', {className:'rw-muted'});
  const notes = el('div');
  const decisions = el('div');
  const auditView = el('div');
  const auditStatus = el('p', {className:'rw-muted', role:'status'});
  let auditResult = null, auditRaw = '', auditTicket = 0, auditStateKey = '';
  const stateKey = data => JSON.stringify([data?.id,data?.experiments.map(a=>[a.id,a.status]),data?.notes.length,data?.decisions?.length]);
  const decision = el('select', {'aria-label':'Candidate decision'},
    ...['accepted','rejected','archived','candidate'].map(value => el('option',{value},value)));
  const decisionReason = el('textarea', {'aria-label':'Decision reason',placeholder:'Explain the selection decision using the saved evidence'});
  const expr = el('textarea', {'aria-label':'Factor expression', placeholder:'-cs_rank(ts_returns(close,5))'});
  const hypothesis = el('textarea', {'aria-label':'Experiment hypothesis', placeholder:'Explain the candidate and expected behavior'});
  const parent = el('select', {'aria-label':'Parent experiment'});
  const note = el('textarea', {'aria-label':'Handoff note', placeholder:'Summarize evidence, failures and the next research step'});
  const cancelReason = el('input', {'aria-label':'Cancellation reason', placeholder:'Why should this experiment stop?'});
  const live = el('input', {type:'checkbox', checked:true, 'aria-label':'Refresh every 3 seconds'});
  const button = (text, action) => el('button', {type:'button', onClick:action}, text);
  const path = (id=sid) => '/sessions/' + encodeURIComponent(id);
  const active = () => root.contains(host);
  const fail = e => { if(active()) error.textContent = e.message || String(e); };
  const short = id => id ? id.slice(0,16) + '…' : 'none';
  const metric = v => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(4) : 'unavailable';
  function inspect(id) {
    selected = id;
    const a = state?.experiments.find(a => a.id === id);
    selectedSummary.textContent = a ? `${a.id} · ${a.status}` : 'Select an experiment to inspect its evidence.';
    detail.textContent = a ? JSON.stringify(a, null, 2) : 'Select an experiment to inspect its saved evidence.';
  }
  function draw(data) {
    state = data;
    if(auditResult && auditResult.session_id !== data.id) {auditTicket++;auditResult=null;auditStatus.textContent='';auditView.replaceChildren();}
    if(auditResult && auditStateKey !== stateKey(data)) auditStatus.textContent = 'Session changed. Run audit again to inspect the latest evidence.';
    if(retry?.experimentId && data.experiments.some(a => a.id === retry.experimentId && a.status !== 'running')) retry = null;
    objective.replaceChildren(el('p', {}, data.goal), el('p', {className:'rw-muted'},
      `Remaining attempts: ${data.remaining_experiments} / ${data.budget} · Dataset: ${data.dataset.name}`),
      el('details', {}, el('summary', {}, 'Dataset configuration'), el('pre', {className:'rw-detail'}, JSON.stringify(data.dataset,null,2))));
    const oldParent = parent.value;
    parent.replaceChildren(el('option', {value:''}, 'No parent'));
    attempts.replaceChildren(); lineage.replaceChildren();
    const ids = new Set(data.experiments.map(a => a.id));
    for (const a of data.experiments) {
      parent.append(el('option', {value:a.id}, `${short(a.id)} · ${a.status}`));
      const report = a.report || {};
      const p = report.portfolio;
      const failure = report.failure_mode || a.error?.message || a.error?.type;
      const card = el('article', {className:'rw-attempt '+a.status},
        button(`${a.status} · ${short(a.id)}`, () => inspect(a.id)),
        el('p', {className:'rw-muted'}, `${a.agent} · ${a.started_at || a.created_at || ''}`),
        el('p', {}, a.hypothesis), el('code', {}, a.expr),
        el('p', {}, `RankIC ${metric(report.rank_ic)} · Rank turnover ${metric(report.turnover_1d)}`));
      if(p) card.append(el('p',{},`Net return ${metric(p.total_return)} · Sharpe ${metric(p.sharpe)} · Cost drag ${metric(p.cost_drag)}`));
      if (failure) card.append(el('p', {className:'rw-error'}, String(failure)));
      if (report.suggestion) card.append(el('p', {}, String(report.suggestion)));
      attempts.append(card);
      const edge = el('div', {className:'rw-lineage'});
      if(a.parent_id) edge.append(ids.has(a.parent_id) ? button(short(a.parent_id), () => inspect(a.parent_id)) : el('span', {}, short(a.parent_id)+' (external)'), el('span', {}, '→ revision →'));
      else edge.append(el('span', {className:'rw-muted'}, 'Root →'));
      edge.append(button(short(a.id)+' · '+a.status, () => inspect(a.id)));
      if(a.replay_of) edge.append(el('span', {className:'rw-muted'}, 'Replay of '+a.replay_of));
      lineage.append(edge);
    }
    parent.value = ids.has(oldParent) ? oldParent : '';
    if(!data.experiments.length) attempts.append(el('p', {className:'rw-muted'}, 'No experiments yet. Connect an agent or evaluate a candidate below.'));
    notes.replaceChildren(...data.notes.map(n => el('article', {className:'rw-attempt'}, el('small', {}, n.agent), el('p', {style:{whiteSpace:'pre-wrap'}}, n.text))));
    decisions.replaceChildren(...(data.decisions || []).map(d => el('article',{className:'rw-attempt'},
      el('small',{},`${d.decision} · ${d.agent}`),el('p',{},d.reason),
      button(short(d.experiment_id),()=>inspect(d.experiment_id)))));
    inspect(selected);
    status.textContent = `Loaded ${data.experiments.length} experiments · Updated ${new Date().toLocaleTimeString()}`;
  }
  async function refresh() {
    if(!sid) return;
    const target = sid, ticket = ++revision;
    const data = await api.research(path(target));
    if(active() && target === sid && ticket === revision) draw(data);
  }
  async function loadSessions(preferred=sid) {
    const version = selection, ticket = ++listRequest;
    const rows = await api.research('/sessions');
    if(!active() || ticket !== listRequest) return;
    if(version !== selection) preferred = sid;
    sessions.replaceChildren(el('option',{value:''},'Select a research session'), ...rows.map(s => el('option',{value:s.id},s.goal)));
    sid = rows.some(s => s.id === preferred) ? preferred : (rows.at(-1)?.id || '');
    sessions.value = sid;
    await refresh();
  }
  async function mutate(action) {
    if(busy) return;
    busy = true; error.textContent = '';
    const controls = [create, evaluate, saveNote, saveDecision, reconcileJobs, cancelSelected, sessions]; controls.forEach(b => b.disabled = true);
    try { await action(); } catch(e) { fail(e); }
    finally { busy = false; controls.forEach(b => b.disabled = false); }
  }
  const create = button('Create research session', () => mutate(async () => {
    if(!datasets.value || !goal.value.trim()) throw new Error('Choose a dataset and enter a research goal.');
    const result = await api.research('/sessions', {dataset_id:datasets.value, goal:goal.value, max_experiments:Number(budget.value)});
    selected = null; retry = null;
    await loadSessions(result.id);
  }));
  const evaluate = button('Evaluate candidate · human action', () => mutate(async () => {
    if(!sid || !expr.value.trim() || !hypothesis.value.trim()) throw new Error('Select a session and enter an expression and hypothesis.');
    const target = sid;
    const body = {expr:expr.value, hypothesis:hypothesis.value, parent_id:parent.value || null};
    const signature = JSON.stringify([target,body]);
    if(!retry || retry.signature !== signature) retry = {signature, id:crypto.randomUUID()};
    status.textContent = 'Evaluation running. Refreshing reads saved state; it does not submit another experiment.';
    const pending = retry;
    const result = await api.research(path(target)+'/experiments', {...body,request_id:pending.id});
    if(retry === pending) {
      if(result.status === 'running') pending.experimentId = result.id;
      else retry = null;
    }
    if(sid === target) {selected=result.id; await refresh();}
  }));
  const saveNote = button('Save handoff note', () => mutate(async () => {
    if(!sid || !note.value.trim()) throw new Error('Select a session and enter a note.');
    await api.research(path()+'/notes',{text:note.value}); note.value=''; await refresh();
  }));
  let pendingDecision = null;
  const saveDecision = button('Save candidate decision',()=>mutate(async()=>{
    if(!selected || !decisionReason.value.trim()) throw new Error('Select an experiment and enter a decision reason.');
    const signature = JSON.stringify([selected,decision.value,decisionReason.value]);
    if(!pendingDecision || pendingDecision.signature !== signature) pendingDecision={signature,id:crypto.randomUUID()};
    await api.research('/experiments/'+encodeURIComponent(selected)+'/decisions',
      {decision:decision.value,reason:decisionReason.value,request_id:pendingDecision.id});
    pendingDecision=null; decisionReason.value=''; await refresh();
  }));
  const reconcileJobs = button('Reconcile asynchronous jobs', () => mutate(async () => {
    if(!sid || !state) throw new Error('Select a session first.');
    const target = sid;
    const pending = state.experiments.filter(a => a.status === 'running' && a.execution);
    for(const a of pending) await api.research('/experiments/'+encodeURIComponent(a.id)+'/reconcile', {});
    if(sid === target) await refresh();
  }));
  const cancelSelected = button('Cancel selected asynchronous experiment', () => mutate(async () => {
    const attempt = state?.experiments.find(a => a.id === selected);
    if(!attempt || attempt.status !== 'running' || !attempt.execution)
      throw new Error('Select a running asynchronous experiment first.');
    if(!cancelReason.value.trim()) throw new Error('Enter a cancellation reason.');
    const target = sid;
    await api.research('/experiments/'+encodeURIComponent(attempt.id)+'/cancel', {reason:cancelReason.value.trim()});
    if(sid === target) {cancelReason.value=''; await refresh();}
  }));
  const exportButton = button('Download manifest', async () => {
    try {
      if(!sid) throw new Error('Select a session first.');
      const target = sid;
      // Preserve numeric JSON representations covered by the manifest hash.
      const raw = await api.researchManifest(target);
      const url = URL.createObjectURL(new Blob([raw],{type:'application/json'}));
      const link = el('a', {href:url,download:target+'.json'}); link.click();
      setTimeout(() => URL.revokeObjectURL(url),1000);
    } catch(e) {fail(e);}
  });
  async function loadAudit(offset=0, expected=null) {
    if(!sid) throw new Error('Select a research session first.');
    const target=sid, ticket=++auditTicket, capturedStateKey=stateKey(state);
    const raw=await api.researchAudit(target,{offset,limit:20,expected_manifest_hash:expected});
    const result=JSON.parse(raw);
    if(!active() || target!==sid || ticket!==auditTicket) return;
    auditResult=result; auditRaw=raw; auditStateKey=capturedStateKey;
    auditStatus.textContent=`${result.status} · Record consistency: ${result.record_integrity} · ${result.finding_counts.errors} errors · ${result.finding_counts.warnings} review items`;
    if(capturedStateKey!==stateKey(state)) auditStatus.textContent+=' · Session changed; run audit again for the latest snapshot.';
    auditView.replaceChildren(
      el('p',{className:'rw-muted'},'Checks saved records without rerunning experiments. Goal achievement and out-of-sample validity require a judge.'),
      el('p',{className:'rw-muted'},'Manifest: '+result.manifest_hash),
      ...result.checks.map(c=>el('p',{},`${c.category}: ${c.status}`)),
      ...result.findings.map(f=>el('article',{className:'rw-attempt'+(f.severity==='error'?' failed':'')},
        el('strong',{},`${f.severity} · ${f.code}`),el('p',{},f.message),
        el('small',{},f.evidence.join(' · ')))),
      el('p',{className:'rw-muted'},`Findings ${Math.min(offset,result.page.finding_total)}–${Math.min(offset+20,result.page.finding_total)} of ${result.page.finding_total}`),
      el('details',{},el('summary',{},'Judge evidence'),el('pre',{className:'rw-detail'},JSON.stringify(result.summary,null,2))));
    const pages=el('div',{className:'rw-actions'});
    if(offset>0) pages.append(button('Previous audit page',()=>loadAudit(Math.max(0,offset-20),result.manifest_hash).catch(fail)));
    if(result.page.has_more || result.page.candidates_have_more) pages.append(button('Next audit page',()=>loadAudit(offset+20,result.manifest_hash).catch(fail)));
    auditView.append(pages);
  }
  const runAudit = button('Audit session',()=>loadAudit().catch(fail));
  const downloadAudit = button('Download audit JSON',()=>{
    if(!auditResult || auditResult.session_id!==sid) {fail(new Error('Run the session audit first.'));return;}
    // Preserve numeric JSON representations covered by audit_hash.
    const url=URL.createObjectURL(new Blob([auditRaw],{type:'application/json'}));
    el('a',{href:url,download:sid+'.audit.json'}).click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  sessions.addEventListener('change', () => {selection++;sid=sessions.value; revision++; selected=null; retry=null; auditTicket++; auditResult=null;auditStatus.textContent='';auditView.replaceChildren(); error.textContent=''; if(sid) refresh().catch(fail); else {state=null;objective.replaceChildren();attempts.replaceChildren();lineage.replaceChildren();notes.replaceChildren();detail.textContent='';selectedSummary.textContent='';parent.replaceChildren();}});
  const panel = (title,...children) => el('section',{className:'rw-panel'},el('h2',{},title),...children);
  host.append(style, el('h1',{},'Research Workbench'),
    el('p',{className:'rw-muted'},'Persistent research shared with MCP agents. Human actions are labeled; saved experiment records are not a live model reasoning trace.'),
    el('div',{className:'rw-toolbar'},sessions,button('Refresh sessions',()=>loadSessions().catch(fail)),el('label',{},live,' Auto-refresh'),reconcileJobs,exportButton),status,error,
    el('div',{className:'rw-grid'},
      panel('Research objective',objective,el('details',{},el('summary',{},'Start a new session'),
        el('label',{},'Dataset',datasets),goal,el('label',{},'Attempt budget',budget),create),
        el('details',{},el('summary',{},'Evaluate a candidate'),hypothesis,expr,el('label',{},'Revision parent',parent),evaluate)),
      panel('Experiment activity',attempts),
      panel('Experiment lineage',lineage),
      panel('Research audit',el('div',{className:'rw-actions'},runAudit,downloadAudit),auditStatus,auditView),
      panel('Evidence & handoff',selectedSummary,el('details',{},el('summary',{},'Full experiment record'),detail),
        el('details',{},el('summary',{},'Cancel an asynchronous experiment'),
          el('p',{className:'rw-muted'},'Cancellation stops queued work and rejects late results. Remote computation may still finish.'),
          cancelReason,cancelSelected),
        el('details',{},el('summary',{},'Candidate decisions'),decisions,decision,decisionReason,saveDecision),
        notes,note,saveNote)));
  (async () => {
    try {
      const rows = await api.research('/datasets');
      if(!active()) return;
      datasets.replaceChildren(...rows.map(d => el('option',{value:d.id},d.name)));
      if(!rows.length) status.textContent='No registered datasets. Prepare the offline demo or register a dataset using MCP/SDK.';
      await loadSessions();
    } catch(e) {fail(e);}
  })();
  async function tick() {
    if(!active()) return;
    if(live.checked && sid) {try {await refresh();} catch(e) {fail(e);}}
    if(active()) setTimeout(tick,3000);
  }
  setTimeout(tick,3000);
}
