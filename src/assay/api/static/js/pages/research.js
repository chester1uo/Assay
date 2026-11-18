// Research workbench: real persisted evidence, no simulated agent actions.
const ZH = {
  "Research session": "研究会话",
  "Dataset": "数据集",
  "Research goal": "研究目标",
  "Describe the hypothesis and research objective": "描述研究假设与目标",
  "Experiment budget": "实验次数预算",
  "Candidate decision": "候选因子决定",
  "Decision reason": "决定理由",
  "Explain the selection decision using the saved evidence": "依据已保存的证据说明选择理由",
  "Factor expression": "因子表达式",
  "Experiment hypothesis": "实验假设",
  "Explain the candidate and expected behavior": "说明候选因子及预期表现",
  "Parent experiment": "父实验",
  "Handoff note": "交接笔记",
  "Summarize evidence, failures and the next research step": "总结证据、失败原因与下一步研究",
  "Cancellation reason": "取消原因",
  "Why should this experiment stop?": "说明取消实验的原因",
  "Refresh every 3 seconds": "每 3 秒刷新",
  "Select an experiment to inspect its evidence.": "选择实验以查看证据。",
  "Select an experiment to inspect its saved evidence.": "选择实验以查看已保存的证据。",
  "Session changed. Run audit again to inspect the latest evidence.": "会话已更改，请重新审计以查看最新证据。",
  "Dataset configuration": "数据集配置",
  "No parent": "无父实验",
  "No experiments yet. Connect an agent or evaluate a candidate below.": "暂无实验。可连接 Agent，或在下方评估候选因子。",
  "Select a research session": "选择研究会话",
  "Create research session": "创建研究会话",
  "Choose a dataset and enter a research goal.": "请选择数据集并输入研究目标。",
  "Evaluate candidate · human action": "评估候选因子 · 人工操作",
  "Select a session and enter an expression and hypothesis.": "请选择会话，并输入因子表达式与实验假设。",
  "Evaluation running. Refreshing reads saved state; it does not submit another experiment.": "实验运行中。刷新只读取已保存的状态，不会重复提交。",
  "Save handoff note": "保存交接笔记",
  "Select a session and enter a note.": "请选择会话并输入笔记。",
  "Save candidate decision": "保存候选因子决定",
  "Select an experiment and enter a decision reason.": "请选择实验并填写决定理由。",
  "Reconcile asynchronous jobs": "同步异步任务状态",
  "Select a session first.": "请先选择会话。",
  "Cancel selected asynchronous experiment": "取消所选异步实验",
  "Select a running asynchronous experiment first.": "请先选择运行中的异步实验。",
  "Enter a cancellation reason.": "请输入取消原因。",
  "Download manifest": "下载研究清单",
  "Select a research session first.": "请先选择研究会话。",
  "Checks saved records without rerunning experiments. Goal achievement and out-of-sample validity require a judge.": "检查已保存的记录，不会重新运行实验。研究目标是否达成及样本外有效性仍需评估。",
  "Previous audit page": "审计上一页",
  "Next audit page": "审计下一页",
  "Judge evidence": "评估证据",
  "Audit session": "审计会话",
  "Download audit JSON": "下载审计 JSON",
  "Run the session audit first.": "请先运行会话审计。",
  "Research Workbench": "研究工作台",
  "Persistent research shared with MCP agents. Human actions are labeled; saved experiment records are not a live model reasoning trace.": "与 MCP Agent 共享的持久研究工作区。人工操作会标记在记录中；实验记录不包含模型实时推理。",
  "Refresh sessions": "刷新会话",
  " Auto-refresh": " 自动刷新",
  "Research objective": "研究目标",
  "Start a new session": "创建新会话",
  "Attempt budget": "实验次数预算",
  "Evaluate a candidate": "评估候选因子",
  "Revision parent": "修订的父实验",
  "Experiment activity": "实验记录",
  "Experiment lineage": "实验关系",
  "Research audit": "研究审计",
  "Evidence & handoff": "证据与交接",
  "Full experiment record": "完整实验记录",
  "Cancel an asynchronous experiment": "取消异步实验",
  "Cancellation stops queued work and rejects late results. Remote computation may still finish.": "取消将停止排队任务并拒绝迟到的结果；远程计算可能仍会结束。",
  "Candidate decisions": "候选因子决定",
  "No registered datasets. Prepare the offline demo or register a dataset using MCP/SDK.": "尚未注册数据集。请准备离线演示数据，或通过 MCP / SDK 注册数据集。",
  "none": "无",
  "unavailable": "不可用",
  "accepted": "已采纳",
  "rejected": "已拒绝",
  "archived": "已归档",
  "candidate": "候选",
  "running": "运行中",
  "completed": "已完成",
  "failed": "失败",
  "cancelled": "已取消",
  "abandoned": "已放弃",
  "Remaining attempts": "剩余实验次数",
  "Rank turnover": "排序换手率",
  "Net return": "净收益",
  "Cost drag": "成本拖累",
  " (external)": "（外部）",
  "→ revision →": "→ 修订 →",
  "Root →": "起点 →",
  "Replay of ": "重放自 ",
  "Loaded": "已加载",
  "experiments": "项实验",
  "Updated": "更新于",
  "Record consistency": "记录一致性",
  "passed": "通过",
  "errors": "项错误",
  "review items": "项待复核",
  " · Session changed; run audit again for the latest snapshot.": " · 会话已更改，请重新审计。",
  "Manifest: ": "清单：",
  "Findings": "审计项",
  "of": "共"
};

let draft = null;

export function render(root, {api, el, lang, setLanguageBusy}) {
  const tr = text => lang === "zh" ? (ZH[text] ?? text) : text;
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
  let sid = draft?.sid || '', state = null, selected = draft?.selected || null, busy = false,
    revision = 0, selection = 0, listRequest = 0, retry = draft?.retry || null;
  let pendingParent = draft?.parent || '';
  const status = el('p', {role:'status', 'aria-live':'polite'});
  const error = el('p', {className:'rw-error', role:'alert'});
  const sessions = el('select', {'aria-label':tr('Research session')});
  const datasets = el('select', {'aria-label':tr('Dataset')});
  const goal = el('textarea', {'aria-label':tr('Research goal'), placeholder:tr('Describe the hypothesis and research objective')});
  const budget = el('input', {type:'number', min:1, value:6, 'aria-label':tr('Experiment budget')});
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
  const decision = el('select', {'aria-label':tr('Candidate decision')},
    ...['accepted','rejected','archived','candidate'].map(value => el('option',{value},tr(value))));
  const decisionReason = el('textarea', {'aria-label':tr('Decision reason'),placeholder:tr('Explain the selection decision using the saved evidence')});
  const expr = el('textarea', {'aria-label':tr('Factor expression'), placeholder:'-cs_rank(ts_returns(close,5))'});
  const hypothesis = el('textarea', {'aria-label':tr('Experiment hypothesis'), placeholder:tr('Explain the candidate and expected behavior')});
  const parent = el('select', {'aria-label':tr('Parent experiment')});
  const note = el('textarea', {'aria-label':tr('Handoff note'), placeholder:tr('Summarize evidence, failures and the next research step')});
  const cancelReason = el('input', {'aria-label':tr('Cancellation reason'), placeholder:tr('Why should this experiment stop?')});
  const live = el('input', {type:'checkbox', checked:true, 'aria-label':tr('Refresh every 3 seconds')});
  if (draft) {
    goal.value = draft.goal; budget.value = draft.budget; decision.value = draft.decision;
    decisionReason.value = draft.decisionReason; expr.value = draft.expr;
    hypothesis.value = draft.hypothesis; note.value = draft.note;
    cancelReason.value = draft.cancelReason; live.checked = draft.live;
  }
  const button = (text, action) => el('button', {type:'button', onClick:action}, text);
  const path = (id=sid) => '/sessions/' + encodeURIComponent(id);
  const active = () => root.contains(host);
  const fail = e => { if(active()) error.textContent = e.message || String(e); };
  const short = id => id ? id.slice(0,16) + '…' : tr('none');
  const metric = v => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(4) : tr('unavailable');
  function inspect(id) {
    selected = id;
    const a = state?.experiments.find(a => a.id === id);
    selectedSummary.textContent = a ? `${a.id} · ${tr(a.status)}` : tr('Select an experiment to inspect its evidence.');
    detail.textContent = a ? JSON.stringify(a, null, 2) : tr('Select an experiment to inspect its saved evidence.');
  }
  function draw(data) {
    state = data;
    if(auditResult && auditResult.session_id !== data.id) {auditTicket++;auditResult=null;auditStatus.textContent='';auditView.replaceChildren();}
    if(auditResult && auditStateKey !== stateKey(data)) auditStatus.textContent = tr('Session changed. Run audit again to inspect the latest evidence.');
    if(retry && data.experiments.some(a => (a.id === retry.experimentId || a.request_id === retry.id) && a.status !== 'running')) retry = null;
    if(pendingDecision && (data.decisions || []).some(d => d.request_id === pendingDecision.id)) pendingDecision = null;
    objective.replaceChildren(el('p', {}, data.goal), el('p', {className:'rw-muted'},
      `${tr('Remaining attempts')}: ${data.remaining_experiments} / ${data.budget} · ${tr('Dataset')}: ${data.dataset.name}`),
      el('details', {}, el('summary', {}, tr('Dataset configuration')), el('pre', {className:'rw-detail'}, JSON.stringify(data.dataset,null,2))));
    const oldParent = parent.value || pendingParent;
    parent.replaceChildren(el('option', {value:''}, tr('No parent')));
    attempts.replaceChildren(); lineage.replaceChildren();
    const ids = new Set(data.experiments.map(a => a.id));
    for (const a of data.experiments) {
      parent.append(el('option', {value:a.id}, `${short(a.id)} · ${tr(a.status)}`));
      const report = a.report || {};
      const p = report.portfolio;
      const failure = report.failure_mode || a.error?.message || a.error?.type;
      const card = el('article', {className:'rw-attempt '+a.status},
        button(`${tr(a.status)} · ${short(a.id)}`, () => inspect(a.id)),
        el('p', {className:'rw-muted'}, `${a.agent} · ${a.started_at || a.created_at || ''}`),
        el('p', {}, a.hypothesis), el('code', {}, a.expr),
        el('p', {}, `RankIC ${metric(report.rank_ic)} · ${tr('Rank turnover')} ${metric(report.turnover_1d)}`));
      if(p) card.append(el('p',{},`${tr('Net return')} ${metric(p.total_return)} · Sharpe ${metric(p.sharpe)} · ${tr('Cost drag')} ${metric(p.cost_drag)}`));
      if (failure) card.append(el('p', {className:'rw-error'}, String(failure)));
      if (report.suggestion) card.append(el('p', {}, String(report.suggestion)));
      attempts.append(card);
      const edge = el('div', {className:'rw-lineage'});
      if(a.parent_id) edge.append(ids.has(a.parent_id) ? button(short(a.parent_id), () => inspect(a.parent_id)) : el('span', {}, short(a.parent_id)+tr(' (external)')), el('span', {}, tr('→ revision →')));
      else edge.append(el('span', {className:'rw-muted'}, tr('Root →')));
      edge.append(button(short(a.id)+' · '+tr(a.status), () => inspect(a.id)));
      if(a.replay_of) edge.append(el('span', {className:'rw-muted'}, tr('Replay of ')+a.replay_of));
      lineage.append(edge);
    }
    parent.value = ids.has(oldParent) ? oldParent : '';
    pendingParent = '';
    if(!data.experiments.length) attempts.append(el('p', {className:'rw-muted'}, tr('No experiments yet. Connect an agent or evaluate a candidate below.')));
    notes.replaceChildren(...data.notes.map(n => el('article', {className:'rw-attempt'}, el('small', {}, n.agent), el('p', {style:{whiteSpace:'pre-wrap'}}, n.text))));
    decisions.replaceChildren(...(data.decisions || []).map(d => el('article',{className:'rw-attempt'},
      el('small',{},`${tr(d.decision)} · ${d.agent}`),el('p',{},d.reason),
      button(short(d.experiment_id),()=>inspect(d.experiment_id)))));
    inspect(selected);
    status.textContent = `${tr('Loaded')} ${data.experiments.length} ${tr('experiments')} · ${tr('Updated')} ${new Date().toLocaleTimeString()}`;
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
    sessions.replaceChildren(el('option',{value:''},tr('Select a research session')), ...rows.map(s => el('option',{value:s.id},s.goal)));
    sid = rows.some(s => s.id === preferred) ? preferred : (rows.at(-1)?.id || '');
    sessions.value = sid;
    await refresh();
  }
  async function mutate(action) {
    if(busy) return;
    busy = true; error.textContent = '';
    setLanguageBusy?.(true);
    const controls = [create, evaluate, saveNote, saveDecision, reconcileJobs, cancelSelected, sessions]; controls.forEach(b => b.disabled = true);
    try { await action(); } catch(e) { fail(e); }
    finally { busy = false; controls.forEach(b => b.disabled = false); setLanguageBusy?.(false); }
  }
  const create = button(tr('Create research session'), () => mutate(async () => {
    if(!datasets.value || !goal.value.trim()) throw new Error(tr('Choose a dataset and enter a research goal.'));
    const result = await api.research('/sessions', {dataset_id:datasets.value, goal:goal.value, max_experiments:Number(budget.value)});
    selected = null; retry = null;
    await loadSessions(result.id);
  }));
  const evaluate = button(tr('Evaluate candidate · human action'), () => mutate(async () => {
    if(!sid || !expr.value.trim() || !hypothesis.value.trim()) throw new Error(tr('Select a session and enter an expression and hypothesis.'));
    const target = sid;
    const body = {expr:expr.value, hypothesis:hypothesis.value, parent_id:parent.value || null};
    const signature = JSON.stringify([target,body]);
    if(!retry || retry.signature !== signature) retry = {signature, id:crypto.randomUUID()};
    status.textContent = tr('Evaluation running. Refreshing reads saved state; it does not submit another experiment.');
    const pending = retry;
    const result = await api.research(path(target)+'/experiments', {...body,request_id:pending.id});
    if(retry === pending) {
      if(result.status === 'running') pending.experimentId = result.id;
      else retry = null;
    }
    if(sid === target) {selected=result.id; await refresh();}
  }));
  const saveNote = button(tr('Save handoff note'), () => mutate(async () => {
    if(!sid || !note.value.trim()) throw new Error(tr('Select a session and enter a note.'));
    await api.research(path()+'/notes',{text:note.value}); note.value=''; await refresh();
  }));
  let pendingDecision = draft?.pendingDecision || null;
  const saveDecision = button(tr('Save candidate decision'),()=>mutate(async()=>{
    if(!selected || !decisionReason.value.trim()) throw new Error(tr('Select an experiment and enter a decision reason.'));
    const signature = JSON.stringify([selected,decision.value,decisionReason.value]);
    if(!pendingDecision || pendingDecision.signature !== signature) pendingDecision={signature,id:crypto.randomUUID()};
    await api.research('/experiments/'+encodeURIComponent(selected)+'/decisions',
      {decision:decision.value,reason:decisionReason.value,request_id:pendingDecision.id});
    pendingDecision=null; decisionReason.value=''; await refresh();
  }));
  const reconcileJobs = button(tr('Reconcile asynchronous jobs'), () => mutate(async () => {
    if(!sid || !state) throw new Error(tr('Select a session first.'));
    const target = sid;
    const pending = state.experiments.filter(a => a.status === 'running' && a.execution);
    for(const a of pending) await api.research('/experiments/'+encodeURIComponent(a.id)+'/reconcile', {});
    if(sid === target) await refresh();
  }));
  const cancelSelected = button(tr('Cancel selected asynchronous experiment'), () => mutate(async () => {
    const attempt = state?.experiments.find(a => a.id === selected);
    if(!attempt || attempt.status !== 'running' || !attempt.execution)
      throw new Error(tr('Select a running asynchronous experiment first.'));
    if(!cancelReason.value.trim()) throw new Error(tr('Enter a cancellation reason.'));
    const target = sid;
    await api.research('/experiments/'+encodeURIComponent(attempt.id)+'/cancel', {reason:cancelReason.value.trim()});
    if(sid === target) {cancelReason.value=''; await refresh();}
  }));
  const exportButton = button(tr('Download manifest'), async () => {
    try {
      if(!sid) throw new Error(tr('Select a session first.'));
      const target = sid;
      // Preserve numeric JSON representations covered by the manifest hash.
      const raw = await api.researchManifest(target);
      const url = URL.createObjectURL(new Blob([raw],{type:'application/json'}));
      const link = el('a', {href:url,download:target+'.json'}); link.click();
      setTimeout(() => URL.revokeObjectURL(url),1000);
    } catch(e) {fail(e);}
  });
  async function loadAudit(offset=0, expected=null) {
    if(!sid) throw new Error(tr('Select a research session first.'));
    const target=sid, ticket=++auditTicket, capturedStateKey=stateKey(state);
    const raw=await api.researchAudit(target,{offset,limit:20,expected_manifest_hash:expected});
    const result=JSON.parse(raw);
    if(!active() || target!==sid || ticket!==auditTicket) return;
    auditResult=result; auditRaw=raw; auditStateKey=capturedStateKey;
    auditStatus.textContent=`${tr(result.status)} · ${tr('Record consistency')}: ${tr(result.record_integrity)} · ${result.finding_counts.errors} ${tr('errors')} · ${result.finding_counts.warnings} ${tr('review items')}`;
    if(capturedStateKey!==stateKey(state)) auditStatus.textContent+=tr(' · Session changed; run audit again for the latest snapshot.');
    auditView.replaceChildren(
      el('p',{className:'rw-muted'},tr('Checks saved records without rerunning experiments. Goal achievement and out-of-sample validity require a judge.')),
      el('p',{className:'rw-muted'},tr('Manifest: ')+result.manifest_hash),
      ...result.checks.map(c=>el('p',{},`${c.category}: ${c.status}`)),
      ...result.findings.map(f=>el('article',{className:'rw-attempt'+(f.severity==='error'?' failed':'')},
        el('strong',{},`${f.severity} · ${f.code}`),el('p',{},f.message),
        el('small',{},f.evidence.join(' · ')))),
      el('p',{className:'rw-muted'},`${tr('Findings')} ${Math.min(offset,result.page.finding_total)}–${Math.min(offset+20,result.page.finding_total)} ${tr('of')} ${result.page.finding_total}`),
      el('details',{},el('summary',{},tr('Judge evidence')),el('pre',{className:'rw-detail'},JSON.stringify(result.summary,null,2))));
    const pages=el('div',{className:'rw-actions'});
    if(offset>0) pages.append(button(tr('Previous audit page'),()=>loadAudit(Math.max(0,offset-20),result.manifest_hash).catch(fail)));
    if(result.page.has_more || result.page.candidates_have_more) pages.append(button(tr('Next audit page'),()=>loadAudit(offset+20,result.manifest_hash).catch(fail)));
    auditView.append(pages);
  }
  const runAudit = button(tr('Audit session'),()=>loadAudit().catch(fail));
  const downloadAudit = button(tr('Download audit JSON'),()=>{
    if(!auditResult || auditResult.session_id!==sid) {fail(new Error(tr('Run the session audit first.')));return;}
    // Preserve numeric JSON representations covered by audit_hash.
    const url=URL.createObjectURL(new Blob([auditRaw],{type:'application/json'}));
    el('a',{href:url,download:sid+'.audit.json'}).click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  sessions.addEventListener('change', () => {selection++;sid=sessions.value; revision++; selected=null; retry=null; auditTicket++; auditResult=null;auditStatus.textContent='';auditView.replaceChildren(); error.textContent=''; if(sid) refresh().catch(fail); else {state=null;objective.replaceChildren();attempts.replaceChildren();lineage.replaceChildren();notes.replaceChildren();detail.textContent='';selectedSummary.textContent='';parent.replaceChildren();}});
  const panel = (title,...children) => el('section',{className:'rw-panel'},el('h2',{},title),...children);
  host.append(style, el('h1',{},tr('Research Workbench')),
    el('p',{className:'rw-muted'},tr('Persistent research shared with MCP agents. Human actions are labeled; saved experiment records are not a live model reasoning trace.')),
    el('div',{className:'rw-toolbar'},sessions,button(tr('Refresh sessions'),()=>loadSessions().catch(fail)),el('label',{},live,tr(' Auto-refresh')),reconcileJobs,exportButton),status,error,
    el('div',{className:'rw-grid'},
      panel(tr('Research objective'),objective,el('details',{dataset:{draft:'create'}},el('summary',{},tr('Start a new session')),
        el('label',{},tr('Dataset'),datasets),goal,el('label',{},tr('Attempt budget'),budget),create),
        el('details',{dataset:{draft:'evaluate'}},el('summary',{},tr('Evaluate a candidate')),hypothesis,expr,el('label',{},tr('Revision parent'),parent),evaluate)),
      panel(tr('Experiment activity'),attempts),
      panel(tr('Experiment lineage'),lineage),
      panel(tr('Research audit'),el('div',{className:'rw-actions'},runAudit,downloadAudit),auditStatus,auditView),
      panel(tr('Evidence & handoff'),selectedSummary,el('details',{dataset:{draft:'record'}},el('summary',{},tr('Full experiment record')),detail),
        el('details',{dataset:{draft:'cancel'}},el('summary',{},tr('Cancel an asynchronous experiment')),
          el('p',{className:'rw-muted'},tr('Cancellation stops queued work and rejects late results. Remote computation may still finish.')),
          cancelReason,cancelSelected),
        el('details',{dataset:{draft:'decision'}},el('summary',{},tr('Candidate decisions')),decisions,decision,decisionReason,saveDecision),
        notes,note,saveNote)));
  if (draft?.details) host.querySelectorAll('details[data-draft]').forEach(node => { node.open = !!draft.details[node.dataset.draft]; });
  (async () => {
    try {
      const rows = await api.research('/datasets');
      if(!active()) return;
      datasets.replaceChildren(...rows.map(d => el('option',{value:d.id},d.name)));
      if (draft?.dataset && rows.some(d => d.id === draft.dataset)) datasets.value = draft.dataset;
      if(!rows.length) status.textContent=tr('No registered datasets. Prepare the offline demo or register a dataset using MCP/SDK.');
      await loadSessions();
    } catch(e) {fail(e);}
  })();
  async function tick() {
    if(!active()) return;
    if(live.checked && sid) {try {await refresh();} catch(e) {fail(e);}}
    if(active()) setTimeout(tick,3000);
  }
  setTimeout(tick,3000);
  return ({languageChange} = {}) => {
    draft = languageChange ? {sid, selected, retry, pendingDecision, parent:parent.value, dataset:datasets.value,
      goal:goal.value, budget:budget.value, decision:decision.value, decisionReason:decisionReason.value,
      expr:expr.value, hypothesis:hypothesis.value, note:note.value, cancelReason:cancelReason.value,
      live:live.checked, details:Object.fromEntries([...host.querySelectorAll('details[data-draft]')].map(node => [node.dataset.draft,node.open]))} : null;
  };
}
