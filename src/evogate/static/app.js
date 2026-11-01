let state, dbId, page = 'query', selectedTask = null, currentQuery = null, busy = false;
let policyLoaded = false;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = value => value == null ? '未知' : (value * 100).toFixed(1) + '%';
const db = () => state.databases.find(item => item.id === dbId);
const date = timestamp => new Date(timestamp * 1000).toLocaleString('zh-CN', {hour12:false});
const empty = text => `<div class="empty">${esc(text)}</div>`;

async function api(path, data) {
  const response = await fetch('/api/' + path, data == null ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}

async function action(title, fn) {
  if (busy) return;
  busy = true; $('busy-title').textContent = title; $('busy').hidden = false;
  document.querySelectorAll('button, select').forEach(node => node.disabled = true);
  try { await fn(); await refresh(); }
  catch (error) { alert(error.message); }
  finally {
    busy = false; $('busy').hidden = true;
    document.querySelectorAll('button, select').forEach(node => node.disabled = false);
    if (state) render();
  }
}

async function refresh() {
  const nextState = await api('state');
  if (state && JSON.stringify(nextState) === JSON.stringify(state)) return;
  state = nextState;
  if (!state.databases.some(item => item.id === dbId)) dbId = state.databases[0].id;
  render();
}

function navigate(next) {
  page = next;
  document.querySelectorAll('.page').forEach(node => node.hidden = node.id !== 'page-' + page);
  document.querySelectorAll('.nav').forEach(node => node.classList.toggle('active', node.dataset.page === page));
  const labels = {query:['查询工作台','让每一次查询，都有据可循。','连接业务数据，执行 SQL，把可靠的纠错沉淀为可验证的经验。'],feedback:['纠错记录','把一次纠错，变成下一次的依据。','保留正确 SQL、业务说明和原始查询，建立数据库专属反馈。'],skills:['Skill 文档','SKILL.md 自进化','依据开发轨迹修改操作流程，验证后发布，保留完整文档与版本差异。'],reports:['效果对比','让效果，落在可检查的结果上。','同模型、同工具，对比初始 Skill、进化版本与无 Skill。']};
  [$('page-name').textContent,$('heading').textContent,$('description').textContent] = labels[page];
}

function render() {
  const database = db();
  $('database').innerHTML = state.databases.map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('');
  $('database').value = dbId;
  $('mode').textContent = state.model.mode === 'demo' ? '离线演示' : '真实模型';
  $('model-label').textContent = state.model.model;
  $('source').textContent = database.source === 'synthetic' ? '内置示例' : database.source;
  $('version-label').textContent = '当前 ' + database.active;
  $('split-label').textContent = `开发 ${database.counts.training} / 选择 ${database.counts.selection} / 保留 ${database.counts.holdout}`;
  $('schema').textContent = database.schema;
  const active = database.versions.find(v => v.id === database.active);
  $('active-tag').textContent = active.id.toUpperCase();
  $('active-skill').textContent = active.skill || '当前为无 Skill 基础版。完成开发查询和纠错后，可生成候选经验。';
  $('notice').hidden = state.model.mode !== 'demo';
  $('notice').textContent = '离线演示：SQL 和候选 Skill 来自预设脚本，数据库执行与版本流程真实运行。此处准确率仅演示界面与选择机制，不是模型或 BIRD 性能结果。';
  $('examples').innerHTML = database.examples.slice(0,6).map(task => `<button class="example" data-task="${esc(task.id)}">${esc(task.question)}</button>`).join('');
  $('examples').querySelectorAll('button').forEach(button => button.onclick = () => {
    selectedTask = database.examples.find(t => t.id === button.dataset.task);
    $('question').value = selectedTask.question; $('evidence').value = selectedTask.evidence;
  });
  const history = state.queries.filter(q => q.db_id === dbId).slice(-8).reverse();
  $('history').innerHTML = history.map(q => `<div class="history-row"><span class="${q.execution.status === 'success' ? 'success' : 'failure'}">●</span><button class="question" data-query="${esc(q.id)}">${esc(q.question)}</button><span class="badge">${esc(q.version)}</span><span class="muted">${date(q.created)}</span></div>`).join('') || empty('还没有查询。从上方示例开始。');
  $('history').querySelectorAll('[data-query]').forEach(button => button.onclick = () => showQuery(state.queries.find(q => q.id === button.dataset.query)));
  $('feedback-list').innerHTML = state.feedback.filter(f => f.db_id === dbId).slice().reverse().map(f => `<div class="feedback-card"><h3>${esc(f.question)}</h3><p class="muted">${esc(f.note || '用户确认的正确 SQL')} · ${date(f.created)}</p><pre class="sql">${esc(f.reference_sql)}</pre></div>`).join('') || empty('还没有纠错记录。提交查询后可录入正确 SQL。');
  const running = state.jobs.some(job => job.status === 'running');
  $('optimize').disabled = running;
  $('bind-skill').disabled = running;
  $('switch-branch').disabled = running;
  const branches = state.branches.filter(branch => branch.db_id === dbId);
  const priorBranch = $('branch-choice').value;
  $('branch-choice').innerHTML = branches.map(branch => `<option value="${esc(branch.id)}">${esc(branch.skill_id)} · ${esc(branch.id)}${branch.id === database.branch_id ? ' · 当前' : ''}</option>`).join('');
  $('branch-choice').value = branches.some(branch => branch.id === priorBranch) ? priorBranch : database.branch_id;
  $('import-skill').disabled = running;
  $('skill-binding').textContent = `Skill ${database.skill_id} · 分支 ${database.branch_id}`;
  const choice = $('skill-choice').value || database.skill_id;
  $('skill-choice').innerHTML = state.skills.map(skill => `<option value="${esc(skill.id)}">${esc(skill.metadata.name)} · ${esc(skill.id)}</option>`).join('');
  $('skill-choice').value = state.skills.some(skill => skill.id === choice) ? choice : 'sql-analysis';
  if (!policyLoaded) {
    const policy = state.evolution_policy;
    $('auto-evolution').checked = policy.enabled;
    $('batch-size').value = policy.batch_size;
    $('cooldown').value = policy.cooldown_seconds;
    $('daily-attempts').value = policy.max_attempts_per_day;
    $('attempt-calls').value = policy.max_calls_per_attempt;
    policyLoaded = true;
  }
  $('experience-list').innerHTML = state.experiences.filter(e => e.db_id === dbId).slice(-50).reverse().map(e => `<div class="feedback-card"><h3>${esc(e.question)}</h3><p class="muted">${esc(e.signal)} · ${esc(e.failure_kind || '无已确认失败类型')} · ${e.resolved ? '已发布版本通过回放' : '保留供后续回放'}</p><p>${esc(e.note)}</p><details><summary>证据与尝试历史（${e.attempts.length}）</summary><pre class="sql">${esc(JSON.stringify(e, null, 2))}</pre></details></div>`).join('') || empty('查询和纠错后会在这里积累经验。');
  $('jobs').innerHTML = state.jobs.filter(j => j.db_id === dbId).slice().reverse().map(j => `<div class="history-row"><strong>${esc(j.status)}</strong><span>${esc(j.reason)} · ${esc(j.version || '')} ${esc(j.decision || '')}</span><span>${j.model_calls ?? '—'} 次模型调用</span><p class="failure">${esc(j.error || '')}</p></div>`).join('') || empty('尚无后台进化任务。');
  $('versions').innerHTML = database.versions.slice().reverse().map(v => {
    const labels = {baseline:'初始版本',accepted:'验证通过',rejected:'拒绝',pending:'待定'};
    const selection = v.selection;
    return `<div class="panel version-card"><div class="section-title"><h2>${esc(v.id)} ${v.id === database.active ? '· 当前使用' : ''}</h2><span class="badge">${labels[v.status]}</span></div><p class="muted">${esc(v.skill_path || '无 Skill 文件')} ${v.skill_format === 'legacy_text' ? '· 历史文本版本' : ''}</p>${v.skill_path ? `<button class="text-button" data-skill-download="${esc(v.id)}">下载 SKILL.md ↓</button>` : ''}<pre class="sql">${esc(v.skill || '空 Skill：仅使用模型、schema 与工具。')}</pre>${selection ? `<p class="muted">选择集 ${selection.count} 题 · 基础版 ${pct(selection.baseline)} → 候选 ${pct(selection.candidate)} · 原版本 ${pct(selection.current)}</p><p class="muted">${esc(selection.reason)}</p><details><summary>查看版本差异</summary><pre class="sql">${esc(v.diff)}</pre></details>` : ''}${v.id !== database.active && ['accepted','baseline'].includes(v.status) && !running ? `<button class="text-button" data-rollback="${esc(v.id)}">回退到此版本 →</button>` : ''}</div>`;
  }).join('');
  $('versions').querySelectorAll('[data-rollback]').forEach(button => button.onclick = () => action('正在切换版本', () => api('rollback',{db_id:dbId,version_id:button.dataset.rollback})));
  $('versions').querySelectorAll('[data-skill-download]').forEach(button => button.onclick = () => {
    const version = database.versions.find(v => v.id === button.dataset.skillDownload);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([version.skill], {type:'text/markdown;charset=utf-8'}));
    link.download = 'SKILL.md';
    link.click();
    URL.revokeObjectURL(link.href);
  });
  const reports = state.reports.filter(r => r.db_id === dbId);
  $('compare').disabled = reports.length > 0;
  $('compare').textContent = reports.length ? '快照评测已完成' : '冻结快照并评测 ↗';
  $('reports').innerHTML = reports.map(report => {
    const metrics = [['初始 Skill EX',pct(report.baseline)],['自优化版 EX',pct(report.optimized)],['净变化',report.gain == null ? '未知' : (report.gain >= 0 ? '+' : '') + (report.gain*100).toFixed(1)+' pp'],['修复 / 退化',report.improved+' / '+report.regressed]];
    const rows = report.rows.baseline.map((before,i) => {
      const after = report.rows.optimized[i];
      return `<tr><td>${esc(before.task_id)}</td><td>${before.score ?? '未知'}</td><td>${after.score ?? '未知'}</td><td>${esc(after.sql)}</td></tr>`;
    }).join('');
    return `<div class="metrics">${metrics.map(([label,value],i) => `<div class="metric ${i === 2 ? 'accent' : ''}"><div class="label">${label}</div><div class="value">${value}</div></div>`).join('')}</div><div class="panel"><div class="section-title"><h2>${esc(report.db_id)} · ${esc(report.version)}</h2><button class="text-button" data-download="${esc(report.id)}">下载完整 JSON ↓</button></div><p class="report-note">${report.kind === 'synthetic_demo' ? '预设脚本的流程演示，不能作为模型有效性证据。' : '数据库内保留问题评测，不是官方 BIRD 榜单成绩。'} 无 Skill EX ${pct(report.no_skill)} · ${report.count} 个独立输入，${report.known_pairs} 对已知评分。${report.shared_observations ? '当前 Skill 为空，两组共享同一观测。' : ''}</p><p class="report-note">BIRD EX 忽略行序与重复行。${esc(report.metadata.prior_exposure || '')}</p><div class="table-scroll"><table><thead><tr><th>任务</th><th>基础版</th><th>自优化版</th><th>SQL</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }).join('') || empty('尚无冻结对比。建议先完成开发查询和一次 Skill 更新。');
  $('reports').querySelectorAll('[data-download]').forEach(button => button.onclick = () => {
    const report = reports.find(r => r.id === button.dataset.download);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
    link.download = `evogate-${dbId}-${report.version}.json`; link.click(); URL.revokeObjectURL(link.href);
  });
  navigate(page);
}

function showQuery(query) {
  currentQuery = query;
  const result = query.execution;
  const width = Math.max(0,...result.rows.map(row => row.length));
  $('query-result').innerHTML = `<div class="panel result-panel"><div class="section-title"><h2>查询结果</h2><span>${esc(query.version)} · ${query.elapsed_ms} ms · ${query.tokens ?? '—'} tokens</span></div><pre class="sql">${esc(query.sql || '未生成有效 SQL')}</pre>${result.status !== 'success' ? `<p class="failure">${esc(result.status)}：${esc(result.detail)}</p>` : `<div class="table-scroll"><table><thead><tr>${Array.from({length:width},(_,i)=>'<th>列 '+(i+1)+'</th>').join('')}</tr></thead><tbody>${result.rows.map(row=>'<tr>'+row.map(cell=>'<td>'+esc(typeof cell === 'object' ? JSON.stringify(cell) : cell)+'</td>').join('')+'</tr>').join('')}</tbody></table></div><p class="muted">${result.row_count} 行${result.truncated ? '，仅预览前 30 行' : ''} · 执行成功不代表语义已验证</p>`}<details><summary>查看执行轨迹（${query.trace.length}）</summary><pre class="sql">${esc(JSON.stringify(query.trace,null,2))}</pre></details><details><summary>提交正确 SQL 与业务纠正</summary><textarea id="correction-sql" aria-label="正确 SQL" placeholder="输入你确认正确的只读 SQL">${esc(query.sql)}</textarea><input id="correction-note" aria-label="业务纠正说明" placeholder="说明原查询错在哪里、正确口径是什么"><div class="feedback-actions"><button id="save-feedback" class="primary" >保存纠错</button></div></details></div>`;
  $('save-feedback').onclick = () => action('正在验证并保存纠错', async () => {
    await api('feedback',{query_id:currentQuery.id,reference_sql:$('correction-sql').value,note:$('correction-note').value});
    navigate('feedback');
  });
}

document.querySelectorAll('.nav').forEach(button => button.onclick = () => navigate(button.dataset.page));
$('go-skills').onclick = () => navigate('skills');
$('database').onchange = () => {
  dbId = $('database').value; selectedTask = null; currentQuery = null;
  $('question').value = ''; $('evidence').value = ''; $('query-result').innerHTML = ''; render();
};
$('question').oninput = () => selectedTask = null;
$('query-form').onsubmit = event => {
  event.preventDefault();
  action('正在生成并执行 SQL',async () => {
    const query = await api('query',{db_id:dbId,question:$('question').value,evidence:$('evidence').value,baseline:$('baseline').checked,task_id:selectedTask?.id || null});
    showQuery(query);
  });
};
$('optimize').onclick = () => action('正在启动后台进化',() => api('evolve',{db_id:dbId}));
$('compare').onclick = () => action('正在运行快照评测', () => api('compare',{db_id:dbId}));
$('database-form').onsubmit = event => {
  event.preventDefault();
  action('正在连接数据库', async () => {
    const result = await api('databases', {db_id:$('new-db-id').value, path:$('new-db-path').value, context:$('new-db-context').value});
    dbId = result.db_id;
  });
};
$('import-cases').onclick = () => action('正在导入案例', async () => {
  const file = $('cases-file').files[0];
  if (!file) throw new Error('请选择案例 JSON 文件。');
  await api('cases', {db_id:dbId, cases:JSON.parse(await file.text())});
});
$('switch-branch').onclick = () => action('正在切换工作区分支', () => api('skills/switch', {db_id:dbId, branch_id:$('branch-choice').value}));
$('bind-skill').onclick = () => action('正在建立 Skill 分支', () => api('skills/bind', {db_id:dbId, skill_id:$('skill-choice').value}));
$('import-skill').onclick = () => action('正在导入 Skill', async () => {
  const file = $('skill-file').files[0];
  if (!file) throw new Error('请选择 SKILL.md。');
  const skill = await api('skills/import', {document:await file.text()});
  await api('skills/bind', {db_id:dbId, skill_id:skill.id});
});
$('policy-form').onsubmit = event => {
  event.preventDefault();
  action('正在保存进化策略', () => api('evolution/config', {
    enabled:$('auto-evolution').checked, batch_size:Number($('batch-size').value),
    cooldown_seconds:Number($('cooldown').value), max_attempts_per_day:Number($('daily-attempts').value),
    max_calls_per_attempt:Number($('attempt-calls').value)
  }));
};
setInterval(() => { if (!busy) refresh().catch(() => {}); }, 5000);
refresh().catch(error => { $('notice').hidden=false; $('notice').textContent='启动失败：'+error.message; });
