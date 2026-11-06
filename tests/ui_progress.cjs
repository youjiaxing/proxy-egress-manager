// Deterministic DOM regression for slow jobs; no real subscription or network.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require(process.env.JSDOM_PATH||'/tmp/proxy-egress-dom/node_modules/jsdom');
const pause=()=>new Promise(r=>setTimeout(r,5));
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await pause();}throw Error('condition timed out');}
(async()=>{
 const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'});
 const w=dom.window,d=w.document;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 const nativeTimeout=w.setTimeout.bind(w);
 w.setTimeout=(fn,ms)=>nativeTimeout(fn,ms===1000||ms===4000?5:ms);
 let accept, polls=0, submissions=0, mode='running', status=200;
 const state={health:{scheduler_valid:true},groups:[],sources:[],nodes:{},settings:{}};
 w.fetch=async(path,options={})=>{
  if(path==='/api/action'){submissions++;await new Promise(r=>accept=r);return {ok:true,json:async()=>({job:'one'})};}
  if(path.startsWith('/api/jobs/')){
   polls++;
   if(mode==='network')throw Error('offline');
   return {ok:status===200,status,text:async()=>'',json:async()=>({status:mode,message:mode==='error'?'订阅校验失败':'操作已完成'})};
  }
  return {ok:true,json:async()=>path==='/api/state'?state:{csrf:'test'}};
 };
 w.eval(fs.readFileSync('web/app.js','utf8'));
 await until(()=>!d.getElementById('dashboard').hidden);
 function submit(){d.getElementById('add-source').click();d.querySelector('[name=name]').value='private name';d.querySelector('[name=url]').value='https://secret.invalid/token';d.getElementById('dialog-form').dispatchEvent(new w.Event('submit',{cancelable:true}));}
 submit();
 assert.equal(d.getElementById('operations').hidden,false);
 assert.match(d.getElementById('operation-list').textContent,/正在提交/);
 assert.equal(d.getElementById('add-source').disabled,true);
 assert.equal(d.getElementById('operation-list').textContent.includes('secret.invalid'),false);
 accept();
 await until(()=>polls>=3);
 assert.match(d.getElementById('operation-list').textContent,/处理中/);
 d.querySelector('[data-tab=settings]').click();
 assert.equal(d.getElementById('operations').hidden,false);
 mode='network';await until(()=>d.getElementById('operation-list').textContent.includes('自动重试'));
 assert.equal(d.getElementById('add-source').disabled,true);
 mode='done';await until(()=>d.getElementById('operation-list').textContent.includes('处理完成'));
 assert.equal(d.getElementById('add-source').disabled,false);
 d.getElementById('clear-operations').click();assert.equal(d.getElementById('operations').hidden,true);
 mode='error';submit();accept();await until(()=>d.getElementById('operation-list').textContent.includes('失败：订阅校验失败'));
 d.getElementById('clear-operations').click();
 mode='running';status=404;submit();accept();await until(()=>d.getElementById('operation-list').textContent.includes('任务记录已不可用'));
 assert.equal(submissions,3);
 d.getElementById('logout').click();await until(()=>d.getElementById('dashboard').hidden);
 assert.equal(d.getElementById('operation-list').children.length,0);
 dom.window.close();console.log('PASS: immediate feedback, persistent progress, tab navigation, transient network recovery, completion, failure, missing job, logout cleanup');
})().catch(e=>{console.error(e);process.exitCode=1;});
