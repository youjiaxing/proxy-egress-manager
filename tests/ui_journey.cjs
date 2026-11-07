const fs=require('fs'),assert=require('node:assert/strict');
const {JSDOM}=require(process.env.JSDOM_PATH||'/tmp/proxy-egress-dom/node_modules/jsdom');
const pause=()=>new Promise(r=>setTimeout(r,5));
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await pause();}throw Error('journey condition timed out');}
(async()=>{
 const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 const g={id:'1',uid:'g1',name:'组1',node:'a',paused:false,pinned:false,lease_valid:true,tcp_ips:[],udp_ips:[],reason:'双协议美国出口已验证',scope:{mode:'nodes',ids:['s/a','s/b']},endpoint:'pem:1081'};
 const nodes=Object.fromEntries(['a','b'].map(id=>[id,{id,ref:'s/'+id,name:'美国 '+id,source:1,disabled:false}]));
 const state={health:{scheduler_valid:true},groups:[g],nodes,sources:[{slot:1,uid:'s',name:'订阅一',enabled:true}],settings:{}};
 const posts=[];let failSave=true,unknown=false,evaluationHold=false,evaluationDone=false,last,job=0;
 w.fetch=async(path,opt={})=>{
  if(path==='/api/action'){
   last=JSON.parse(opt.body);posts.push(last);
   if(unknown)throw Error('connection lost');
   return {ok:true,json:async()=>({job:String(++job)})};
  }
  if(path.startsWith('/api/jobs/')){
   if(last.kind==='group_evaluate'){
    if(evaluationHold)return {ok:true,json:async()=>({status:'running'})};
    evaluationDone=true;return {ok:true,json:async()=>({status:'done',result:{kind:last.kind,group:'1',outcome:'evaluated',message:'评估完成',selection:{recommended:[]}}})};
   }
   if(last.kind==='group_scope'&&failSave)return {ok:true,json:async()=>({status:'error',message:'存储暂不可写，配置没有保存'})};
   if(last.kind==='group_scope')g.scope=last.scope;
   if(last.kind==='group_resume'){g.paused=false;g.lease_valid=true;}
   if(last.kind==='group_pin')g.pinned=last.pinned;
   return {ok:true,json:async()=>({status:'done',result:{kind:last.kind,group:'1',outcome:last.kind==='group_scope'?'saved':'applied',message:'配置已处理'}})};
  }
  return {ok:true,json:async()=>path==='/api/state'?state:path.startsWith('/api/pool')?{group_uid:'g1',summaries:{}}:{csrf:'t'}};
 };
 const timer=w.setTimeout.bind(w);w.setTimeout=(fn,ms)=>timer(fn,ms===1000?5:ms);
 w.eval(fs.readFileSync('web/app.js','utf8')+'\nwindow.refreshForTest=refresh;');
 const submit=()=>d.getElementById('dialog-form').dispatchEvent(new w.Event('submit',{cancelable:true}));
 const click=selector=>d.querySelector(selector).click();
 await until(()=>d.querySelector('[data-configure="1"]'));
 click('[data-configure="1"]');click('[data-scope="1"]');
 assert.equal(d.querySelectorAll('#scope-options input:checked').length,2,'editing reuses saved pool');
 const a=d.querySelector('[value="s/a"]');a.checked=false;a.dispatchEvent(new w.Event('change'));
 assert.match(d.getElementById('scope-selection-summary').textContent,/1 个节点/);
 submit();assert.match(d.getElementById('dialog-title').textContent,/确认分组配置/);
 assert.equal(posts.length,0,'review is not a save');
 submit();assert.equal(posts.length,0,'removing current node requires confirmation');
 assert.match(d.getElementById('pool-error').textContent,/确认断开/);
 click('[data-flow-back]');assert.equal(d.querySelector('[value="s/a"]').checked,false,'back retains draft');
 submit();d.querySelector('[name=confirm_disconnect]').checked=true;submit();
 await until(()=>d.getElementById('dialog-title').textContent==='配置未完成');
 assert.match(d.getElementById('dialog-fields').textContent,/存储暂不可写/);
 assert.match(d.getElementById('group-list').textContent,/本次操作未完成/);
 click('[data-flow-back]');assert.equal(d.querySelector('[value="s/a"]').checked,false,'failure retains draft');
 failSave=false;submit();d.querySelector('[name=confirm_disconnect]').checked=true;submit();
 await until(()=>d.getElementById('dialog-title').textContent.includes('分组可用'));
 assert.deepEqual(g.scope,{mode:'nodes',ids:['s/b']});assert.match(d.getElementById('dialog-fields').textContent,/实际状态/);
 click('#dialog-cancel');
 // Saving paused configuration should lead to enabling it in the same journey.
 g.paused=true;g.lease_valid=false;g.node='b';await w.refreshForTest();
 click('[data-configure="1"]');click('[data-scope="1"]');submit();submit();
 await until(()=>d.querySelector('[data-flow-action="group_resume"]'));
 click('[data-flow-action="group_resume"]');await until(()=>d.getElementById('dialog-title').textContent.includes('分组可用'));
 assert.equal(g.paused,false);click('#dialog-cancel');
 g.pinned=true;await w.refreshForTest();click('[data-configure="1"]');click('[data-scope="1"]');submit();submit();
 await until(()=>d.querySelector('[data-flow-action="group_pin"]'));click('[data-flow-action="group_pin"]');
 await until(()=>posts.some(p=>p.kind==='group_pin'));assert.equal(posts.at(-1).pinned,false);
 await until(()=>d.getElementById('dialog-title').textContent.includes('分组可用'));click('#dialog-cancel');
 // An ambiguous submission must not silently retry or claim a saved configuration.
 unknown=true;click('[data-configure="1"]');click('[data-scope="1"]');submit();submit();
 await until(()=>d.getElementById('dialog-title').textContent.includes('无法确认'));
 const n=posts.length;await pause();assert.equal(posts.length,n);assert.match(d.getElementById('dialog-fields').textContent,/不要重复提交/);
 assert.equal(d.querySelector('[data-flow-action]'),null);
 click('[data-flow-refresh]');await until(()=>d.getElementById('dialog-title').textContent.includes('已核对'));
 assert.ok(d.querySelector('[data-flow-back]'));assert.equal(posts.length,n,'checking state must not repeat ambiguous write');
 click('#dialog-cancel');
 unknown=false;evaluationHold=true;w.confirm=()=>true;
 click('[data-configure="1"]');click('[data-evaluate="1"]');await until(()=>posts.at(-1).kind==='group_evaluate');
 click('#dialog-cancel');evaluationHold=false;await until(()=>evaluationDone);await pause();
 assert.equal(d.getElementById('dialog').open,false,'closing a background evaluation must not reopen the journey');
 await pause();dom.window.close();
 console.log('PASS configuration journey: saved-pool editing, review before write, disconnect consent, back/failure preserve draft, visible outcome, paused enable, fixed unlock, ambiguous writes never retried');
})().catch(e=>{console.error(e);process.exit(1)});
