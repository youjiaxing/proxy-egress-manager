'use strict';
let csrf = '', state = null, activeTab = 'groups', refreshTimer, dialogSubmit;
const operations = new Map();
let layout='cards', sourceFilter='', qualityContext=null, qualitySequence=0;
const selectedNodes=new Set();
try {layout=localStorage.getItem('pem-layout')==='table'?'table':'cards';}catch{}

let operationSession = 0;
const operationNames = {source_add:'添加订阅', source_update:'编辑订阅', source_refresh:'刷新订阅', source_toggle:'更改订阅状态', source_delete:'删除订阅', group_switch:'切换出口', group_check:'检测出口', group_resume:'恢复分组', group_pause:'暂停分组', group_pin:'更改节点固定方式', group_scope:'编辑可用节点清单', group_retry:'立即重试', group_add:'新建分组', group_delete:'删除分组', settings:'保存设置', node_update:'更新节点'};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '尚未验证';
function message(text) { $('message').textContent = text; $('message').hidden = false; clearTimeout(message.timer); message.timer = setTimeout(() => $('message').hidden = true, 7000); }
async function api(path, body, method) {
  const response = await fetch(path, {method: method || (body === undefined ? 'GET' : 'POST'), credentials:'same-origin', headers:body === undefined ? {} : {'Content-Type':'application/json','X-CSRF-Token':csrf}, body:body === undefined ? undefined : JSON.stringify(body)});
  if (response.status === 401) showLogin();
  if (!response.ok) { let text = await response.text(); try { text = JSON.parse(text).error || text; } catch {} const error=new Error(text || '请求失败'); error.status=response.status; throw error; }
  return response.json();
}
function showLogin() { closeQuality(); selectedNodes.clear(); hideHelp(); renderGroups.previous=null; operationSession++; operations.clear(); renderOperations(); closeDialog(); $('login').hidden = false; $('dashboard').hidden = true; $('logout').hidden = true; clearInterval(refreshTimer); }
async function authenticated() { $('login').hidden = true; $('dashboard').hidden = false; $('logout').hidden = false; await refresh(); clearInterval(refreshTimer); refreshTimer = setInterval(() => refresh().catch(e => message(e.message)), 8000); }
async function action(kind, data = {}) {
  const key = [kind, data.group || '', data.slot || '', data.node || ''].join(':');
  if ([...operations.values()].some(o=>o.key===key && o.running)) { message('该操作正在处理，请查看操作进度'); return; }
  const operation = {key, title:operationNames[kind] || '管理操作', started:Date.now(), running:true, detail:'正在提交…', session:operationSession};
  operations.set(operation, operation); renderOperations();
  try {
    const {job} = await api('/api/action', {kind,...data});
    if (operation.session !== operationSession) return;
    await waitForJob(job, operation);
  } catch (error) {
    if (operation.session !== operationSession) return;
    operation.running=false; operation.detail='未能确认结果：'+error.message+'。请先查看页面状态，勿重复提交。'; renderOperations();
  }
}
function renderOperations() {
  const items=[...operations.values()];
  $('operations').hidden=!items.length;
  $('operation-list').innerHTML=items.map(o=>`<li><strong>${esc(o.title)}</strong><span>${esc(o.detail)}</span>${o.running?`<small>已等待 ${Math.floor((Date.now()-o.started)/1000)} 秒</small>${o.cancelJob?`<button class="quiet" data-cancel-test="${esc(o.cancelJob)}">取消测试</button>`:''}`:''}</li>`).join('');
  $('add-source').disabled=items.some(o=>o.running && o.key?.startsWith('source_add:'));
}
async function waitForJob(job, operation) {
  operation ||= {title:'恢复备份', started:Date.now(), running:true, session:operationSession};
  operations.set(operation, operation);
  operation.detail='已提交，排队或处理中；节点检测可能需要数分钟。'; renderOperations();
  while (operation.session === operationSession) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    if (operation.session !== operationSession) return;
    let result;
    try { result = await api('/api/jobs/' + job); }
    catch (error) {
      if (operation.session !== operationSession) return;
      if(error.status===400 || error.status===404) { operation.running=false; operation.detail='任务记录已不可用，无法确认结果，请查看订阅或分组当前状态。'; renderOperations(); return; }
      operation.detail='暂时无法查询进度，将自动重试；这不代表操作失败，请勿重复提交。'; renderOperations();
      await new Promise(resolve=>setTimeout(resolve,4000));
      continue;
    }
    if (operation.session !== operationSession) return;
    if (result.status !== 'running') {
      operation.running=false;
      operation.detail=result.status==='error'?'失败：'+result.message:'处理完成。';
      if(result.result?.length) {operation.detail+=' 已完成 '+result.result.length+' 个节点，详情见节点连接质量。';}
      if(operation.diagnostic && qualityContext) await renderQuality();
      try {
        await refresh();
        if (result.status==='done' && operation.key?.startsWith('group_')) {
          const group=state.groups.find(g=>g.id===operation.key.split(':')[1]);
          if(group) operation.detail='处理完成：'+(group.lease_valid?'可用':'暂不可用')+'；'+groupReason(group.reason);
        }
      } catch { operation.detail+=' 页面状态暂时刷新失败，请稍后重试。'; }
      if (operation.session !== operationSession) return;
      renderOperations(); return;
    }
    operation.detail=operation.diagnostic&&result.message&&result.message!=='操作已排队'?result.message:'排队或处理中；节点检测可能需要数分钟，请勿重复提交。'; renderOperations();
  }
}
async function refresh() {
  state = await api('/api/state');
  const ok = state.health.scheduler_valid && !state.health.maintenance;
  $('maintenance-notice').hidden = !state.health.maintenance;
  $('health').textContent = ok ? '● 后台检测运行中' : '● 后台检测暂时失联'; $('health').className = 'badge ' + (ok ? 'active' : 'paused');
  document.querySelectorAll('[data-layout]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.layout===layout)));
  const active = state.groups.filter(g => g.lease_valid).length;
  const ips = new Set(state.groups.flatMap(g => [...g.tcp_ips,...g.udp_ips]));
  $('summary').innerHTML = [[active+'/'+state.groups.length,'可用分组'],[state.sources.filter(s=>s.enabled).length,'启用的订阅来源'],[Object.keys(state.nodes).length,'候选节点'],[ips.size,'已记录的出口 IP']].map(([v,t])=>`<div class="metric"><strong>${esc(v)}</strong><span>${t}</span></div>`).join('');
  updateSourceFilter(); renderGroups(); renderSources(); if (!document.activeElement?.matches('#node-list input')) renderNodes();
  if (activeTab === 'history') await renderEvents();
  if (!document.querySelector('#settings input:focus, #settings textarea:focus')) fillSettings();
}
function helpButton(control, label, attributes, help) {
  return `<button class="quiet" data-control="${esc(control)}" data-help="${esc(help)}" ${attributes}>${esc(label)}</button>`;
}
let helpTarget = null, helpTimer;
function hideHelp() {
  clearTimeout(helpTimer);
  if(helpTarget) helpTarget.removeAttribute('aria-describedby');
  helpTarget=null; $('help-tooltip').hidden=true;
}
function showHelp(target) {
  if(!target?.dataset.help) return;
  hideHelp(); helpTarget=target;
  const tip=$('help-tooltip'); tip.textContent=target.dataset.help; tip.hidden=false;
  target.setAttribute('aria-describedby','help-tooltip');
  const rect=target.getBoundingClientRect(), box=tip.getBoundingClientRect();
  tip.style.left=Math.max(12,Math.min(rect.left,innerWidth-box.width-12))+'px';
  tip.style.top=Math.max(12,rect.bottom+box.height+10<=innerHeight ? rect.bottom+8 : rect.top-box.height-8)+'px';
}
function groupReason(reason) {
  const meanings = {
    '等待双协议验证':'正在等待 TCP 和 UDP 连接验证，通过后才能使用。',
    '启动后等待重验':'服务刚启动，正在重新验证节点。',
    '手动暂停':'你已暂停此分组，点击“启用分组”后才会继续验证和使用。',
    '节点不可用或订阅已停用':'尚无可用的当前节点，或其订阅已停用；系统会按设置寻找合格节点。',
    '没有已验证的独立美国备用出口，保留占用等待恢复':'本轮暂未找到通过全部检查、且与其他组出口不重复的美国节点；该组目前不能使用，系统会继续检查。',
    '双协议美国出口已验证':'TCP 和 UDP 均已验证为美国出口，且与其他组已记录的出口不重复。',
    '钉住节点不合格，暂停且不切换':'当前节点未通过检查；由于你固定了节点，该组停止转发，不自动更换。',
    '出口身份到期；继续独立连通探测':'上次出口验证已过期，正在重新检查；通过前暂停使用。',
    '出口身份到期':'上次出口验证已过期，通过重新检查前暂停使用。',
    '调度授权到期':'后台检测未能及时确认运行正常，已停止转发，等待检测恢复。',
    '组间出口冲突，等待串行重验':'检测到不同分组可能共用同一出口，已停止使用并重新检查。',
    '已观测组间出口冲突':'当前出口与其他分组重复，已停止使用并寻找合格节点。',
    '组间出口冲突':'当前出口与其他分组重复，已停止使用并重新检查。',
    '非美国出口':'检测到当前节点的出口不在美国，不能使用。',
    '已观测非美国出口':'检测到当前节点的出口不在美国，不能使用。',
    '观测已超过身份有效期，等待下一次重验':'这次检测结果已过期，需要重新验证后才能使用。',
    '旧会话仍在清理，等待下一轮重验':'正在关闭旧连接，完成后会重新验证。',
    '订阅节点改变或消失，重验前暂停':'订阅中的当前节点已改变或被移除，重新验证前暂停使用。',
    '后端映射变化，重新选择并验证':'实际连接的节点发生变化，正在重新选择和验证。',
    '出口/节点更新：清理旧会话后提交占用':'正在更换节点或出口，先关闭旧连接，再验证是否可以使用。',
    '观测出口变化/异常，清理会话并等待完整提交':'检测到出口变化或异常，先关闭旧连接，验证通过后再使用。'
  };
  return meanings[reason] || reason;
}
function renderGroups() {
  const markup = state.groups.map(g => {
    const n = state.nodes[g.node], source = n && state.sources.find(s=>s.slot===n.source);
    const status = g.paused ? '已手动暂停' : g.lease_valid ? '可用' : '暂不可用';
    const buttons = [
      helpButton('check-'+g.id,'检查连接',`data-action="group_check" data-group="${esc(g.id)}"`,'安排一次全组连接和出口验证；手动暂停的组仍保持暂停。必要时系统可能寻找替代节点。'),
      helpButton('switch-'+g.id,'更换节点',`data-switch="${esc(g.id)}"`,'自动选择或指定新的代理节点，验证合格后才使用。切换会断开已有连接；请先启用分组并解除固定。'),
      helpButton('pause-'+g.id,g.paused?'启用分组':'暂停分组',`data-action="${g.paused?'group_resume':'group_pause'}" data-group="${esc(g.id)}"`,g.paused?'重新启用并等待验证，验证通过前仍不能使用。':'停止这个代理入口并断开已有连接，直到你再次启用。'),
      helpButton('pin-'+g.id,g.pinned?'允许自动换节点':'固定当前节点',`data-pin="${esc(g.id)}" aria-disabled="${!g.pinned&&!g.node}"`,g.pinned?'解除固定；正常时保持当前节点，满足切换条件后才寻找替代节点。':!g.node?'请先等待分组获得节点，再固定当前节点。':'保持当前节点，不自动更换；故障或验证未通过时停止转发。固定节点不代表固定公网 IP。'),
      helpButton('scope-'+g.id,'可用清单',`data-scope="${esc(g.id)}"`,'指定本组允许使用的订阅或节点；移出当前节点会断开连接。'),
      helpButton('quality-'+g.id,'连接质量',`data-quality-group="${esc(g.id)}"`,'查看运行检查历史，或独立测试当前节点；手动测试不修改业务分组。'),
      helpButton('retry-'+g.id,'立即重试',`data-action="group_retry" data-group="${esc(g.id)}"`,'在允许范围内立即尝试一轮；没有合格节点后按1、5、15分钟放慢。'),
      helpButton('delete-'+g.id,'删除分组',`data-delete-group="${esc(g.id)}"`,'删除该代理入口并断开已有连接。使用此地址的业务需要重新配置；订阅不会删除。')
    ].join('');
    const scopeText = describeScope(g);
    const retryText = g.next_retry_at>Date.now()/1000 ? `下次恢复检查：${date(g.next_retry_at)}，等待期间不重复探测` : '';
    if(layout==='table') return `<tr><td><strong>${esc(g.name)}</strong><br><span class="badge ${g.lease_valid?'active':'paused'}">${status}</span><br>${esc(n?.name||'无合格节点')}<br><small>${esc(g.endpoint)}</small></td><td>${esc(scopeText)}<br><small>${g.candidate_count??0} 个有效候选</small></td><td>TCP ${esc(g.tcp_ips.join(', ')||'尚未验证')}<br>UDP ${esc(g.udp_ips.join(', ')||'尚未验证')}<p>${esc(groupReason(g.reason))}</p><small>${esc(retryText)}</small></td><td><div class="table-actions">${buttons}</div></td></tr>`;
    return `<article><div class="section-title"><h2>${esc(g.name)}</h2><span class="badge ${g.lease_valid?'active':'paused'}">${status}</span></div><p class="endpoint-label">业务中填写的代理地址</p><div class="endpoint">socks5://${esc(g.endpoint)}</div><dl class="details"><dt>当前节点 / 订阅</dt><dd>${esc(n?.name || '尚未找到合格节点')} / ${esc(source?.name || '—')}</dd><dt>出口地址（TCP）</dt><dd>${esc(g.tcp_ips.join(', ')||'尚未验证')}</dd><dt>出口地址（UDP）</dt><dd>${esc(g.udp_ips.join(', ')||'尚未验证')}</dd><dt>上次验证时间</dt><dd>${date(g.verified_at)}</dd><dt>当前选择已保持</dt><dd>${g.since?Math.floor((Date.now()/1000-g.since)/60)+' 分钟':'—'}</dd><dt>允许使用范围</dt><dd>${esc(scopeText)} · ${g.candidate_count??0} 个有效候选</dd><dt>自动更换节点</dt><dd>${g.pinned ? (g.pin_until?'已关闭，至 '+date(g.pin_until):'已关闭，需手动允许'):'已允许，正常时保持当前节点'}</dd></dl><div class="actions">${buttons}</div><p class="reason">当前情况：${esc(groupReason(g.reason))}<br>${esc(retryText)}</p></article>`;
  }).join('') || '<p class="empty">尚无分组，创建后会验证可用节点。</p>';
  // Unchanged polling results should not interrupt reading a tooltip or keyboard navigation.
  if(markup===renderGroups.previous) return;
  const focused=document.activeElement?.dataset.control;
  hideHelp(); $('group-list').className=layout==='cards'?'grid':'table-wrap'; $('group-list').innerHTML=layout==='cards'?markup:`<table class="group-table"><thead><tr><th>分组 / 当前节点</th><th>允许范围</th><th>运行状态</th><th>操作</th></tr></thead><tbody>${markup}</tbody></table>`; renderGroups.previous=markup;
  if(focused) [...$('group-list').querySelectorAll('[data-control]')].find(b=>b.dataset.control===focused)?.focus({preventScroll:true});
}
function renderSources() {
  $('source-list').innerHTML = state.sources.map(s=>`<article><div class="section-title"><h2>${esc(s.name)}</h2><span class="badge ${s.enabled?'active':'paused'}">${s.enabled?'启用':'停用'}</span></div><p class="muted">来源 ${s.slot} · ${Object.values(state.nodes).filter(n=>n.source===s.slot).length} 节点<br>上次刷新：${date(s.refreshed_at)}<br>${esc(s.error||'订阅秘密已隐藏')}</p><div class="actions"><button data-view-source="${s.slot}">查看节点</button><button class="quiet" data-source-edit="${s.slot}">编辑</button><button data-source-refresh="${s.slot}">刷新 / 导入</button><button class="quiet" data-source-toggle="${s.slot}">${s.enabled?'停用':'启用'}</button><button class="quiet" data-source-delete="${s.slot}">删除</button></div></article>`).join('') || '<p class="empty">添加 HTTPS 订阅，或直接导入 Mihomo YAML / URI 文本。</p>';
}
function describeScope(g) {
  const scope=g.scope||{mode:'all',ids:[]};
  if(scope.mode==='all')return '全部已启用订阅';
  return scope.ids.map(id=>scope.mode==='sources'?(state.sources.find(s=>s.uid===id)?.name||'已删除的订阅'):(Object.values(state.nodes).find(n=>n.ref===id)?.name||'已失效节点')).join('、')||'暂无有效选择';
}
function updateSourceFilter(){
  const select=$('source-filter'); if(!select)return;
  const options='<option value="">全部订阅</option>'+state.sources.map(s=>`<option value="${s.slot}">${esc(s.name)}</option>`).join('');
  if(select.innerHTML!==options)select.innerHTML=options;
  select.value=sourceFilter;
}
function visibleNodes(){const filter=$('node-filter').value.toLowerCase(),usOnly=$('us-only').checked;return Object.values(state.nodes).filter(n=>(!sourceFilter||String(n.source)===sourceFilter)&&(!usOnly||n.hint_us||n.last_probe?.us)&&(n.name.toLowerCase().includes(filter)||(state.sources.find(s=>s.slot===n.source)?.name||'').toLowerCase().includes(filter)));}
function nodeSummary(n){
  const p=n.last_probe,t=n.last_test;
  const metric=t?.test==='http'?t.samples?.find(s=>s.method==='http-get-headers'):p?.samples?.find(s=>s.method==='http-get-headers');
  const when=t?.test==='http'?t.time:p?.time;
  return `${metric?.ok?esc(metric.ms)+' ms':'请求耗时：—'}<br><small>${when?date(when)+(Date.now()/1000-when>180?' · 旧记录':''):'未检测'} · 不是 ping</small><br>${esc(p?.reason||'尚未完成出口验证')}`;
}
function renderNodes() {
  if(!state)return;
  const nodes=visibleNodes();
  $('node-table').hidden=layout==='cards';$('node-cards').hidden=layout!=='cards';
  const select=n=>`<input class="node-select" type="checkbox" data-select-node="${esc(n.ref||n.id)}" aria-label="选择 ${esc(n.name)}" ${selectedNodes.has(n.ref||n.id)?'checked':''}>`;
  const actions=n=>`<button class="quiet" data-quality-node="${esc(n.ref||n.id)}">连接质量 / 测试</button><button class="quiet" data-disable-node="${esc(n.id)}">${n.disabled?'启用':'禁用'}</button>`;
  $('node-list').innerHTML=nodes.map(n=>`<tr><td>${select(n)}<strong>${esc(n.name)}</strong><br><small>${esc(state.sources.find(s=>s.slot===n.source)?.name||'来源 '+n.source)}</small></td><td>${nodeSummary(n)}</td><td>TCP ${esc(n.last_probe?.tcp_ips.join(', ')||'—')}<br>UDP ${esc(n.last_probe?.udp_ips.join(', ')||'—')}</td><td><input aria-label="节点优先级" type="number" min="0" max="10000" value="${n.priority}" data-priority="${esc(n.id)}"></td><td>${actions(n)}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">没有符合条件的节点</td></tr>';
  $('node-cards').innerHTML=nodes.map(n=>`<article><h2>${select(n)}${esc(n.name)}</h2><p class="muted">${esc(state.sources.find(s=>s.slot===n.source)?.name||'来源 '+n.source)}</p><p>${nodeSummary(n)}</p><label>节点优先级<input type="number" aria-label="节点优先级" min="0" max="10000" value="${n.priority}" data-priority="${esc(n.id)}"></label><div class="actions">${actions(n)}</div></article>`).join('')||'<p class="empty">没有符合条件的节点</p>';
}
async function renderEvents() {
  const events = await api('/api/events');
  $('event-list').innerHTML = events.map(e=>`<div class="event"><time>${date(e.time)}</time><span>${esc(e.group_id?'组 '+e.group_id:e.kind)}</span><span>${esc(e.message)}</span></div>`).join('') || '<p class="empty">还没有事件。</p>';
}
function fillSettings() {
  for (const el of $('settings-form').elements) if (el.name && state.settings[el.name] !== undefined) el.value = Array.isArray(state.settings[el.name]) ? state.settings[el.name].join('\n') : state.settings[el.name];
  $('alerts-form').elements.alert_enabled.checked = state.settings.alert_enabled;
  $('alerts-form').elements.alert_url.placeholder = state.settings.alert_configured?'已配置；留空保留':'https://…';
}
function formDialog(title, fields, submit, submitLabel = '确认') {
  hideHelp();
  $('dialog-title').textContent = title; $('dialog-fields').innerHTML = fields; $('dialog-form').querySelector('[type=submit]').textContent = submitLabel; dialogSubmit = submit; $('dialog').showModal();
}
function clearDialog() { $('dialog-fields').replaceChildren(); dialogSubmit = null; }
function closeDialog() { if ($('dialog').open) $('dialog').close(); clearDialog(); }
function guarded(fn) { return (...args) => Promise.resolve(fn(...args)).catch(e=>message(e.message)); }
$('login-form').onsubmit = guarded(async e=>{e.preventDefault(); const password=e.target.elements.password.value; const result=await api('/api/login',{password}); csrf=result.csrf; e.target.reset(); await authenticated();});
$('logout').onclick = guarded(async()=>{await api('/api/logout',{}); csrf=''; showLogin();});
$('clear-operations').onclick=()=>{for(const [key,o] of operations) if(!o.running) operations.delete(key); renderOperations();};
for (const b of document.querySelectorAll('[data-tab]')) b.onclick=guarded(async()=>{activeTab=b.dataset.tab; document.querySelectorAll('.tab').forEach(el=>el.hidden=el.id!==activeTab); document.querySelectorAll('[data-tab]').forEach(el=>el.classList.toggle('active',el===b)); if(activeTab==='history') await renderEvents();});
$('node-filter').oninput = renderNodes; $('us-only').onchange = renderNodes;
$('source-filter').onchange=e=>{sourceFilter=e.target.value;selectedNodes.clear();renderNodes();};
$('dialog-close').onclick = $('dialog-cancel').onclick = closeDialog;
$('dialog').addEventListener('close',()=>{if (!$('dialog').open) clearDialog();});
$('dialog-form').onsubmit = guarded(async e=>{e.preventDefault(); const formData=new FormData(e.target),values=Object.fromEntries(formData), submit=dialogSubmit; if(formData.has('scope_mode')) values.scope_ids=formData.getAll('ids'); closeDialog(); await submit(values);});
$('add-group').onclick=()=>formDialog('新建分组','<p>每个分组分配一个独立的代理入口，供业务填写。创建后会自动寻找并验证节点。</p><label>分组名称<input name="name" placeholder="例如：业务 A" required maxlength="100"></label>',v=>action('group_add',v));
$('add-source').onclick=()=>formDialog('添加订阅来源','<label>名称<input name="name" required maxlength="100"></label><label>HTTPS 订阅 URL<input name="url" type="url" placeholder="https://…" autocomplete="off"></label><label>或导入文本<textarea name="content" rows="7" placeholder="proxies: 或 URI 列表"></textarea></label>',v=>action('source_add',v));
document.addEventListener('pointerover',e=>{const b=e.target.closest('[data-help]');if(b || $('help-tooltip').contains(e.target))clearTimeout(helpTimer);if(b && b!==helpTarget) showHelp(b);});
document.addEventListener('pointerout',e=>{const tip=$('help-tooltip');if(helpTarget && (helpTarget.contains(e.target) || tip.contains(e.target)) && !helpTarget.contains(e.relatedTarget) && !tip.contains(e.relatedTarget)) helpTimer=setTimeout(hideHelp,200);});
document.addEventListener('focusin',e=>{const b=e.target.closest('[data-help]');if(b)showHelp(b);else hideHelp();});
document.addEventListener('focusout',e=>{if(e.target===helpTarget)hideHelp();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideHelp();});
window.addEventListener('resize',hideHelp);
window.addEventListener('scroll',hideHelp,true);
document.addEventListener('click',guarded(async e=>{
  const b=e.target.closest('button'); if(!b)return;
  if(b.dataset.layout){layout=b.dataset.layout;try{localStorage.setItem('pem-layout',layout);}catch{}renderGroups.previous=null;await refresh();}
  if(b.dataset.viewSource){sourceFilter=b.dataset.viewSource;$('node-filter').value='';$('us-only').checked=false;$('source-filter').value=sourceFilter;document.querySelector('[data-tab=nodes]').click();renderNodes();}
  if(b.dataset.scope)openScope(b.dataset.scope);
  if(b.dataset.qualityGroup)openQuality({group:b.dataset.qualityGroup});
  if(b.dataset.qualityNode)openQuality({node:b.dataset.qualityNode});
  if(b.dataset.cancelTest)await api('/api/jobs/'+b.dataset.cancelTest+'/cancel',{});
  if(b.dataset.action) await action(b.dataset.action,{group:b.dataset.group});
  if(b.dataset.pin) {
    const g=state.groups.find(g=>g.id===b.dataset.pin);
    if(g.pinned) await action('group_pin',{group:g.id,pinned:false});
    else if(!g.node) message('请先等待分组获得节点，再固定当前节点。');
    else formDialog('固定当前节点','<p>固定后不再自动换节点。当前节点故障或验证不通过时，该组停止转发；固定节点不等于固定公网 IP。</p><label>多久后允许自动换节点（分钟）<input name="minutes" type="number" min="0" value="0"></label><p class="muted">填 0 表示一直固定，直到你手动点击“允许自动换节点”。</p>',v=>action('group_pin',{group:g.id,pinned:true,seconds:Number(v.minutes)*60}),'固定当前节点');
  }
  if(b.dataset.switch) {
    const id=b.dataset.switch, g=state.groups.find(g=>g.id===id);
    if(g.paused) {message('请先点击“启用分组”，再更换节点。');return;}
    if(g.pinned) {message('请先点击“允许自动换节点”，再更换节点。');return;}
    formDialog('更换代理节点','<label>选择代理节点<select name="node"><option value="">系统自动选择（推荐）</option>'+Object.values(state.nodes).filter(n=>!n.disabled&&(!g.candidate_refs||g.candidate_refs.includes(n.ref))).map(n=>`<option value="${esc(n.id)}">${esc(n.name)} · 订阅 ${n.source}</option>`).join('')+'</select></label><p class="muted">系统会检查美国归属、TCP / UDP 连接和与其他组出口是否重复，验证通过后才使用。切换会断开已有连接；若没有合格节点，该组可能仍不可用。</p>',v=>action('group_switch',{group:id,node:v.node||null}),'开始选择并验证');
  }
  if(b.dataset.deleteGroup && confirm('删除这个代理入口并断开已有连接？使用此地址的业务需要重新配置，订阅不会删除。')) await action('group_delete',{group:b.dataset.deleteGroup});
  if(b.dataset.sourceEdit) {
    const slot=Number(b.dataset.sourceEdit), source=await api('/api/sources/'+slot);
    if ($('dashboard').hidden) return;
    formDialog('编辑订阅来源',`<label>名称<input name="name" required maxlength="100" value="${esc(source.name)}"></label><label>HTTPS 订阅 URL<input name="url" type="url" maxlength="4096" autocomplete="off" value="${esc(source.url)}"></label><label>替换节点文本（可选）<textarea name="content" rows="5" placeholder="留空保留现有节点；也可粘贴新内容"></textarea></label><p class="muted">清空 URL 可改为仅手动导入。更换 URL 或导入文本时，校验成功才保存。</p>`,v=>action('source_update',{slot,...v}),'保存');
  }
  if(b.dataset.sourceRefresh) {const slot=Number(b.dataset.sourceRefresh); formDialog('刷新订阅','<p>有 URL 时留空将重新下载；也可粘贴新文本。失败保留旧配置。</p><textarea name="content" rows="7"></textarea>',v=>action('source_refresh',{slot,...v}));}
  if(b.dataset.sourceToggle) await action('source_toggle',{slot:Number(b.dataset.sourceToggle)});
  if(b.dataset.sourceDelete && confirm('删除订阅？使用其节点的组将先暂停。')) await action('source_delete',{slot:Number(b.dataset.sourceDelete)});
  if(b.dataset.disableNode) {const n=state.nodes[b.dataset.disableNode]; await action('node_update',{node:n.id,disabled:!n.disabled});}
}));
document.addEventListener('change',guarded(async e=>{if(e.target.dataset.selectNode){if(e.target.checked)selectedNodes.add(e.target.dataset.selectNode);else selectedNodes.delete(e.target.dataset.selectNode);}if(e.target.dataset.priority) await action('node_update',{node:e.target.dataset.priority,priority:Number(e.target.value)});}));
$('settings-form').onsubmit=guarded(async e=>{e.preventDefault(); const values=Object.fromEntries(new FormData(e.target)); for(const k of ['tcp_check_urls','tcp_identity_urls','stun_servers']) values[k]=values[k].split('\n').map(s=>s.trim()).filter(Boolean); for(const k of ['check_interval','failure_seconds','health_grace','identity_ttl','probe_timeout']) values[k]=Number(values[k]); await action('settings',values);});
$('alerts-form').onsubmit=guarded(async e=>{e.preventDefault(); const data={alert_enabled:e.target.elements.alert_enabled.checked}; if(e.target.elements.alert_url.value) data.alert_url=e.target.elements.alert_url.value; await action('settings',data); e.target.elements.alert_url.value='';});
function download(value,name) {const link=document.createElement('a'); link.href=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})); link.download=name; link.click(); setTimeout(()=>URL.revokeObjectURL(link.href),1000);}
$('backup').onclick=guarded(async()=>{if(confirm('备份含订阅与节点秘密，请勿分享。继续下载？')) download(await api('/api/backup',{}),'pem-backup-'+new Date().toISOString().slice(0,10)+'.json');});
$('diagnostics').onclick=guarded(async()=>download(await api('/api/diagnostics'),'pem-diagnostics.json'));
$('restore').onclick=guarded(async()=>{const f=$('restore-file').files[0]; if(!f) throw new Error('请先选择备份'); if(!confirm('恢复配置会停止全部代理并覆盖现有配置。旧版本备份将恢复为全部订阅范围；新增质量历史保留并标记恢复时间。确认已保存当前配置？')) return; const {job}=await api('/api/restore',JSON.parse(await f.text())); await waitForJob(job);});

function openScope(id){
  const g=state.groups.find(g=>g.id===id),scope=g.scope||{mode:'all',ids:[]};
  formDialog('可用节点清单',`<p>当前节点：${esc(state.nodes[g.node]?.name||'无合格节点')}。正常时保持，故障后只在此范围内选择。</p><label>允许使用的范围<select name="scope_mode" id="scope-mode"><option value="all">全部已启用订阅</option><option value="sources">指定订阅（含以后新增节点）</option><option value="nodes">指定节点（配置变化需重新选择）</option></select></label><input id="scope-search" type="search" placeholder="筛选可选择的订阅或节点"><div id="scope-options" class="scope-options"></div><div id="scope-notice" class="notice"></div><label><input type="checkbox" name="confirm_disconnect">若当前节点被移出，我确认断开已有连接</label><p class="muted">固定或手动暂停不会因保存清单而解除。失效的旧选择会保留显示，不自动扩大范围。</p>`,v=>action('group_scope',{group:id,scope:{mode:v.scope_mode,ids:v.scope_ids||[]},confirm_disconnect:v.confirm_disconnect==='on'}),'保存清单');
  $('scope-mode').value=scope.mode;
  const draw=()=>{
    const mode=$('scope-mode').value;
    const choices=mode==='sources'?state.sources.map(s=>({id:s.uid,name:s.name})):Object.values(state.nodes).map(n=>({id:n.ref,name:n.name+' · '+(state.sources.find(s=>s.slot===n.source)?.name||'')}));
    if(scope.mode===mode)for(const id of scope.ids)if(!choices.some(x=>x.id===id))choices.push({id,name:'已失效选择（保留以避免自动扩大范围）'});
    $('scope-options').innerHTML=mode==='all'?'<p>使用全部已启用订阅的未禁用节点。</p>':choices.map(x=>`<label><input type="checkbox" name="ids" value="${esc(x.id)}" ${scope.mode===mode&&scope.ids.includes(x.id)?'checked':''}>${esc(x.name)}</label>`).join('');
    $('scope-notice').textContent='当前节点被移出时会先断开连接，再从新范围验证选择；找不到合格节点就暂停。';
  };
  $('scope-mode').onchange=draw;draw();
  $('scope-search').oninput=e=>{const filter=e.target.value.toLowerCase();$('scope-options').querySelectorAll('label').forEach(l=>l.hidden=!l.textContent.toLowerCase().includes(filter));};
}
function closeQuality(){qualitySequence++;qualityContext=null;if($('quality-dialog')?.open)$('quality-dialog').close();}
function openQuality(context){qualityContext={...context,hours:24};$('quality-dialog').showModal();renderQuality().catch(e=>message(e.message));}
function qualityName(){if(!qualityContext)return '';return qualityContext.group?state.groups.find(g=>g.id===qualityContext.group)?.name||'历史分组':Object.values(state.nodes).find(n=>n.ref===qualityContext.node)?.name||'已变更节点';}
function chart(points){
  const vals=points.filter(p=>p.ms!==null);if(!vals.length)return '<p class="muted">没有成功耗时样本；失败不填成 0 ms。</p>';
  const first=points[0].time,last=points[points.length-1].time,max=Math.max(...vals.map(p=>p.ms),1);let path='',prev=null;
  for(const p of points){if(p.ms===null){prev=null;continue;}const x=20+(p.time-first)/Math.max(last-first,1)*550,y=110-p.ms/max*85;path+=(prev&&!p.break_before&&p.time-prev.time<=90?'L':'M')+x.toFixed(1)+' '+y.toFixed(1)+' ';prev=p;}
  return `<svg class="quality-chart" viewBox="0 0 600 135" role="img" aria-label="请求耗时趋势；失败与缺测分段"><path d="M20 10V112H580" fill="none" stroke="currentColor" opacity=".2"/><path d="${path}" fill="none" stroke="currentColor" stroke-width="2"/><text x="22" y="12" fill="currentColor" font-size="10">${esc(Math.round(max))} ms</text><text x="20" y="130" fill="currentColor" font-size="10">${esc(date(first))}</text><text x="400" y="130" fill="currentColor" font-size="10">${esc(date(last))}</text></svg>`;
}
async function renderQuality(){
  if(!qualityContext)return;
  const seq=++qualitySequence,ctx={...qualityContext};
  $('quality-title').textContent=qualityName()+' · 连接质量';
  $('quality-body').innerHTML='<p role="status">正在读取历史…</p>';
  const query=new URLSearchParams({hours:ctx.hours,...(ctx.group?{group:ctx.group}:{node:ctx.node})});
  const data=await api('/api/quality?'+query);
  if(seq!==qualitySequence||!qualityContext)return;
  const g=ctx.group&&state.groups.find(g=>g.id===ctx.group),n=ctx.group?state.nodes[g?.node]:Object.values(state.nodes).find(n=>n.ref===ctx.node);
  const currentRef=n?.ref;
  const manual=(data.manual_records||data.records||[]).find(r=>r.origin==='manual'&&r.node_ref===currentRef);
  const udp=(data.udp_records||data.records||[]).find(r=>r.origin==='manual'&&r.test==='udp'&&r.node_ref===currentRef);
  const states=[...(data.state_records||(data.records||[]).filter(r=>r.origin==='state')),...(data.restores||[]).map(r=>({...r,status:'unknown'}))].sort((a,b)=>b.time-a.time);
  const series=data.series||[];
  const available=g?`业务入口：${g.lease_valid?'当前可用':g.paused?'已手动暂停':'暂不可用'} · 当前节点：${n?.name||'无合格节点'}`:'节点检测与完整美国出口验证是不同结果。';
  $('quality-body').innerHTML=`<p class="notice">${esc(available)}</p><div class="actions"><button id="quality-24" class="${ctx.hours===24?'':'quiet'}">最近 24 小时</button><button id="quality-168" class="${ctx.hours===168?'':'quiet'}">最近 7 天</button><button id="quality-refresh" class="quiet">刷新记录</button></div><p class="muted">实际最早记录：${data.coverage_start?date(data.coverage_start):'尚无记录'}。最多 7 天 / 5 万条 / 32 MiB 记录内容，数据库另有开销；达到上限会提前清理。${esc(data.error||'')}</p><div class="actions"><button id="quality-http" ${!currentRef?'disabled':''}>独立检查当前节点连接</button><button id="quality-udp" class="quiet" ${!currentRef?'disabled':''}>手动 UDP 诊断</button></div><p class="status-note">仅点击后发出测试；使用独立进程，不切换业务节点。不是现有业务会话或完整网关链路的测速，也不能保证测得相同出口。正在测试时可在页面“操作进度”查看和取消。</p>${manual?`<p>最近独立测试：${date(manual.time)} · ${manual.current===false?'节点已变化，仅作历史记录':esc(manual.error||'已完成；不等于完整出口验证合格')}</p>`:''}<div class="quality-section"><h2>手动 UDP 往返诊断</h2>${udp?.sent?`<div class="quality-stats"><div class="quality-stat"><strong>${udp.received} / ${udp.sent}</strong><small>收到 / 已发样本</small></div><div class="quality-stat"><strong>${udp.unanswered_pct}%</strong><small>往返未响应比例</small></div><div class="quality-stat"><strong>${udp.mean_ms===null?'—':udp.mean_ms+' ms'}</strong><small>成功样本平均往返</small></div></div><p>目标：${esc(udp.target)} · ${date(udp.time)}</p><p>成功样本最小 / 最大：${udp.min_ms??'—'} / ${udp.max_ms??'—'} ms</p>`:'<p>所选时间范围内尚无本节点的有效 UDP 诊断结果。未检测不显示成 0% 丢包。</p>'}<p class="status-note">默认 20 个请求，不自动重传。只代表到指定目标的往返未响应，无法区分去程、回程或目标原因，不代表所有业务的真实丢包率。</p></div><div class="quality-section"><h2>按目标分别查看检查结果</h2><p class="status-note">运行检查、候选选路、独立手动测试分开统计。耗时包括代理建连及目标响应；UDP 日常检查含已有重试。空档不当成正常，成功率不是丢包率，也不是业务可用时间比例。</p>${series.length?series.map(s=>`<div class="quality-series"><h3>${esc(s.target)}</h3><p>${esc(s.origin==='running'?'分组运行检查':s.origin==='candidate'?'选路时候选检查':'独立手动测试')} · ${esc(s.node_ref===currentRef?'当前节点':Object.values(state.nodes).find(n=>n.ref===s.node_ref)?.name||'历史节点')} · ${esc(s.method==='http-get-headers'?'HTTP 响应头耗时':'UDP STUN（含重试）')}</p><div class="quality-stats"><div class="quality-stat"><strong>${s.mean_ms??'—'}${s.mean_ms!==null?' ms':''}</strong><small>成功样本平均耗时</small></div><div class="quality-stat"><strong>${s.successes} / ${s.attempts}</strong><small>检查成功 ${s.attempts?Math.round(s.successes/s.attempts*1000)/10:0}%</small></div><div class="quality-stat"><strong>${s.variation_ms??'—'}${s.variation_ms!==null?' ms':''}</strong><small>相邻成功耗时差的平均值</small></div></div>${chart(s.points)}<p class="status-note">波动不跨失败或超过 90 秒的缺测样本计算；这不是线路抖动或丢包测量。</p></div>`).join(''):'<p>尚无质量样本。未使用节点不定时巡检；运行组会随原有检查逐步积累。</p>'}</div><div class="quality-section"><h2>状态变化与不可用记录</h2><p class="status-note">记录是采样观察，不提供精确故障起止或 SLA。无观测不等于线路断网；手动暂停单独注明。</p><p class="status-note">${data.state_truncated?'状态变化仅展示最新 5000 条，完整保留记录可单独导出。':''}</p><div class="quality-events">${states.map(r=>`<p>${date(r.time)} · ${esc(r.reason)} · ${r.status==='available'?'验证可用':'停止转发或等待验证'}</p>`).join('')||'所选时间范围内暂无状态变化记录'}</div></div>`;
  for(const h of [24,168])$('quality-'+h).onclick=()=>{qualityContext.hours=h;renderQuality().catch(e=>message(e.message));};
  $('quality-refresh').onclick=()=>renderQuality().catch(e=>message(e.message));
  $('quality-http').onclick=guarded(()=>runTests([currentRef],'http',ctx.group));
  $('quality-udp').onclick=guarded(()=>runTests([currentRef],'udp',ctx.group));
}
async function runTests(refs,test='http',group){
  if(!refs.length||refs.some(r=>!r))throw Error('请先选择有效节点');
  if(refs.length>100)throw Error('单次最多 100 个节点，请筛选后分批测试');
  const key='test:'+test+':'+refs.join(',');
  if([...operations.values()].some(o=>o.key===key&&o.running)){message('这项测试已在进行中');return;}
  if(refs.length>1&&!confirm(`将按顺序测试 ${refs.length} 个节点，只发送轻量请求。超时节点可能使等待时间较长。继续？`))return;
  const operation={key,title:test==='udp'?'手动 UDP 诊断':'独立连接检查（'+refs.length+' 个节点）',started:Date.now(),running:true,detail:'正在提交…',session:operationSession,diagnostic:true};
  operations.set(operation,operation);renderOperations();
  try{const {job}=await api('/api/tests',{nodes:refs,test,group});if(operation.session!==operationSession)return;operation.cancelJob=job;await waitForJob(job,operation);}
  catch(e){if(operation.session!==operationSession)return;operation.running=false;operation.detail='未能确认测试结果：'+e.message;renderOperations();}
}
$('quality-close').onclick=closeQuality;
$('quality-dialog').addEventListener('close',()=>{qualitySequence++;qualityContext=null;});
$('test-selected').onclick=guarded(()=>runTests([...selectedNodes]));
$('test-source').onclick=guarded(()=>{if(!sourceFilter)throw Error('请先选择一个订阅');return runTests(Object.values(state.nodes).filter(n=>String(n.source)===sourceFilter&&!n.disabled).map(n=>n.ref));});
$('export-quality').onclick=guarded(async()=>{if(!confirm('质量历史可能包含出口 IP、节点标识和运行时间，请安全保存。继续下载？'))return;const link=document.createElement('a');link.href='/api/quality/export';link.download='pem-quality.ndjson';link.click();});

(async()=>{try {const session=await api('/api/session'); csrf=session.csrf; await authenticated();} catch {showLogin();}})();
