'use strict';
let csrf = '', state = null, activeTab = 'groups', refreshTimer, dialogSubmit;
const operations = new Map();
let operationSession = 0;
const operationNames = {source_add:'添加订阅', source_update:'编辑订阅', source_refresh:'刷新订阅', source_toggle:'更改订阅状态', source_delete:'删除订阅', group_switch:'切换出口', group_check:'检测出口', group_resume:'恢复分组', group_pause:'暂停分组', group_pin:'更改钉住', group_add:'新建分组', group_delete:'删除分组', settings:'保存设置', node_update:'更新节点'};
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
function showLogin() { operationSession++; operations.clear(); renderOperations(); closeDialog(); $('login').hidden = false; $('dashboard').hidden = true; $('logout').hidden = true; clearInterval(refreshTimer); }
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
  $('operation-list').innerHTML=items.map(o=>`<li><strong>${esc(o.title)}</strong><span>${esc(o.detail)}</span>${o.running?`<small>已等待 ${Math.floor((Date.now()-o.started)/1000)} 秒</small>`:''}</li>`).join('');
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
      try {
        await refresh();
        if (result.status==='done' && operation.key?.startsWith('group_')) {
          const group=state.groups.find(g=>g.id===operation.key.split(':')[1]);
          if(group) operation.detail='处理完成：'+(group.lease_valid?'放行中':'未放行')+'；'+group.reason;
        }
      } catch { operation.detail+=' 页面状态暂时刷新失败，请稍后重试。'; }
      if (operation.session !== operationSession) return;
      renderOperations(); return;
    }
    operation.detail='排队或处理中；节点检测可能需要数分钟，请勿重复提交。'; renderOperations();
  }
}
async function refresh() {
  state = await api('/api/state');
  const ok = state.health.scheduler_valid && !state.health.maintenance;
  $('maintenance-notice').hidden = !state.health.maintenance;
  $('health').textContent = ok ? '● 调度授权有效' : '● 调度授权失效'; $('health').className = 'badge ' + (ok ? 'active' : 'paused');
  const active = state.groups.filter(g => g.lease_valid).length;
  const ips = new Set(state.groups.flatMap(g => [...g.tcp_ips,...g.udp_ips]));
  $('summary').innerHTML = [[active+'/'+state.groups.length,'正在放行的分组'],[state.sources.filter(s=>s.enabled).length,'启用的订阅来源'],[Object.keys(state.nodes).length,'候选节点'],[ips.size,'已记录的独立出口地址']].map(([v,t])=>`<div class="metric"><strong>${esc(v)}</strong><span>${t}</span></div>`).join('');
  renderGroups(); renderSources(); if (!document.activeElement?.matches('#node-list input')) renderNodes();
  if (activeTab === 'history') await renderEvents();
  if (!document.querySelector('#settings input:focus, #settings textarea:focus')) fillSettings();
}
function renderGroups() {
  $('group-list').innerHTML = state.groups.map(g => {
    const n = state.nodes[g.node], source = n && state.sources.find(s=>s.slot===n.source);
    const status = g.lease_valid ? '放行中' : '已暂停';
    return `<article><div class="section-title"><h2>${esc(g.name)}</h2><span class="badge ${g.lease_valid?'active':'paused'}">${status}</span></div><div class="endpoint">socks5://${esc(g.endpoint)}</div><dl class="details"><dt>节点 / 来源</dt><dd>${esc(n?.name || '未分配')} / ${esc(source?.name || '—')}</dd><dt>TCP 出口</dt><dd>${esc(g.tcp_ips.join(', ')||'未验证')}</dd><dt>UDP 出口</dt><dd>${esc(g.udp_ips.join(', ')||'未验证')}</dd><dt>验证时间</dt><dd>${date(g.verified_at)}</dd><dt>本次保持</dt><dd>${g.since?Math.floor((Date.now()/1000-g.since)/60)+' 分钟':'—'}</dd><dt>节点钉住</dt><dd>${g.pinned ? (g.pin_until?'至 '+date(g.pin_until):'手动解除前保持'):'未钉住'}</dd></dl><div class="actions"><button data-action="group_check" data-group="${g.id}">检测</button><button class="quiet" data-switch="${g.id}">切换</button><button class="quiet" data-action="${g.paused?'group_resume':'group_pause'}" data-group="${g.id}">${g.paused?'恢复':'暂停'}</button><button class="quiet" data-pin="${g.id}">${g.pinned?'解除钉住':'钉住'}</button><button class="quiet" data-delete-group="${g.id}">删除</button></div><p class="reason">${esc(g.reason)}</p></article>`;
  }).join('') || '<p class="empty">尚无出口组，创建后验证才会放行。</p>';
}
function renderSources() {
  $('source-list').innerHTML = state.sources.map(s=>`<article><div class="section-title"><h2>${esc(s.name)}</h2><span class="badge ${s.enabled?'active':'paused'}">${s.enabled?'启用':'停用'}</span></div><p class="muted">来源 ${s.slot} · ${Object.values(state.nodes).filter(n=>n.source===s.slot).length} 节点<br>上次刷新：${date(s.refreshed_at)}<br>${esc(s.error||'订阅秘密已隐藏')}</p><div class="actions"><button class="quiet" data-source-edit="${s.slot}">编辑</button><button data-source-refresh="${s.slot}">刷新 / 导入</button><button class="quiet" data-source-toggle="${s.slot}">${s.enabled?'停用':'启用'}</button><button class="quiet" data-source-delete="${s.slot}">删除</button></div></article>`).join('') || '<p class="empty">添加 HTTPS 订阅，或直接导入 Mihomo YAML / URI 文本。</p>';
}
function renderNodes() {
  if (!state) return;
  const filter = $('node-filter').value.toLowerCase(), usOnly = $('us-only').checked;
  const nodes = Object.values(state.nodes).filter(n=>(!usOnly || n.hint_us || n.last_probe?.us) && (n.name.toLowerCase().includes(filter) || String(n.source).includes(filter)));
  $('node-list').innerHTML = nodes.map(n=>`<tr><td><strong>${esc(n.name)}</strong><br><small>来源 ${n.source} · ${esc(n.id)}</small></td><td>${esc(n.last_probe?.reason || (n.hint_us?'美国标注 · 待验证':'尚未验证'))}<br><small>${date(n.last_probe?.time)}</small></td><td>TCP ${esc(n.last_probe?.tcp_ips.join(', ')||'—')}<br>UDP ${esc(n.last_probe?.udp_ips.join(', ')||'—')}</td><td><input aria-label="节点优先级" type="number" min="0" max="10000" value="${n.priority}" data-priority="${esc(n.id)}"></td><td><button class="quiet" data-disable-node="${esc(n.id)}">${n.disabled?'启用':'禁用'}</button></td></tr>`).join('') || '<tr><td colspan="5" class="empty">没有符合条件的节点</td></tr>';
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
$('dialog-close').onclick = $('dialog-cancel').onclick = closeDialog;
$('dialog').addEventListener('close',()=>{if (!$('dialog').open) clearDialog();});
$('dialog-form').onsubmit = guarded(async e=>{e.preventDefault(); const values=Object.fromEntries(new FormData(e.target)), submit=dialogSubmit; closeDialog(); await submit(values);});
$('add-group').onclick=()=>formDialog('新建出口组','<label>组名称<input name="name" required maxlength="100"></label>',v=>action('group_add',v));
$('add-source').onclick=()=>formDialog('添加订阅来源','<label>名称<input name="name" required maxlength="100"></label><label>HTTPS 订阅 URL<input name="url" type="url" placeholder="https://…" autocomplete="off"></label><label>或导入文本<textarea name="content" rows="7" placeholder="proxies: 或 URI 列表"></textarea></label>',v=>action('source_add',v));
document.addEventListener('click',guarded(async e=>{
  const b=e.target.closest('button'); if(!b)return;
  if(b.dataset.action) await action(b.dataset.action,{group:b.dataset.group});
  if(b.dataset.pin) {const g=state.groups.find(g=>g.id===b.dataset.pin); if(g.pinned) await action('group_pin',{group:g.id,pinned:false}); else formDialog('钉住节点：故障时暂停，不自动切换','<label>定时解除（分钟，0 表示手动解除）<input name="minutes" type="number" min="0" value="0"></label>',v=>action('group_pin',{group:g.id,pinned:true,seconds:Number(v.minutes)*60}));}
  if(b.dataset.switch) {const id=b.dataset.switch; formDialog('切换到经验证的独立美国出口','<label>候选节点<select name="node"><option value="">自动寻找</option>'+Object.values(state.nodes).filter(n=>!n.disabled).map(n=>`<option value="${esc(n.id)}">${esc(n.name)} · 来源 ${n.source}</option>`).join('')+'</select></label><p class="muted">不会绕过双协议、美国归属和组间独立性检查；切换会断开旧会话。</p>',v=>action('group_switch',{group:id,node:v.node||null}));}
  if(b.dataset.deleteGroup && confirm('删除该组并终止其全部代理会话？')) await action('group_delete',{group:b.dataset.deleteGroup});
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
document.addEventListener('change',guarded(async e=>{if(e.target.dataset.priority) await action('node_update',{node:e.target.dataset.priority,priority:Number(e.target.value)});}));
$('settings-form').onsubmit=guarded(async e=>{e.preventDefault(); const values=Object.fromEntries(new FormData(e.target)); for(const k of ['tcp_check_urls','tcp_identity_urls','stun_servers']) values[k]=values[k].split('\n').map(s=>s.trim()).filter(Boolean); for(const k of ['check_interval','failure_seconds','health_grace','identity_ttl','probe_timeout']) values[k]=Number(values[k]); await action('settings',values);});
$('alerts-form').onsubmit=guarded(async e=>{e.preventDefault(); const data={alert_enabled:e.target.elements.alert_enabled.checked}; if(e.target.elements.alert_url.value) data.alert_url=e.target.elements.alert_url.value; await action('settings',data); e.target.elements.alert_url.value='';});
function download(value,name) {const link=document.createElement('a'); link.href=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})); link.download=name; link.click(); setTimeout(()=>URL.revokeObjectURL(link.href),1000);}
$('backup').onclick=guarded(async()=>{if(confirm('备份含订阅与节点秘密，请勿分享。继续下载？')) download(await api('/api/backup',{}),'pem-backup-'+new Date().toISOString().slice(0,10)+'.json');});
$('diagnostics').onclick=guarded(async()=>download(await api('/api/diagnostics'),'pem-diagnostics.json'));
$('restore').onclick=guarded(async()=>{const f=$('restore-file').files[0]; if(!f) throw new Error('请先选择备份'); if(!confirm('覆盖恢复会停止全部代理并覆盖配置。确认已保存当前备份？')) return; const {job}=await api('/api/restore',JSON.parse(await f.text())); await waitForJob(job);});
(async()=>{try {const session=await api('/api/session'); csrf=session.csrf; await authenticated();} catch {showLogin();}})();
