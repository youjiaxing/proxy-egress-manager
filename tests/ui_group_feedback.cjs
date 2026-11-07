const fs=require('fs'),assert=require('node:assert/strict');
const {JSDOM}=require(process.env.JSDOM_PATH||'/tmp/proxy-egress-dom/node_modules/jsdom');
const pause=()=>new Promise(r=>setTimeout(r,5));
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await pause();}throw Error('condition timed out');}
(async()=>{
 const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 const g={id:'2',uid:'uid2',name:'组2',node:null,paused:false,pinned:false,lease_valid:false,tcp_ips:[],udp_ips:[],scope:{mode:'all',ids:[]},candidate_count:77,reason:'尚无合格节点',endpoint:'proxy-egress-manager:1082'};
 const state={health:{scheduler_valid:true},groups:[g],sources:[],nodes:{},settings:{}};
 let mode='running',cancelled=false;
 const finished={kind:'group_switch',group:'2',group_uid:'uid2',finished:Date.now()/1000+1,outcome:'not_applied',message:'未应用新节点；已检查全部77个候选',selection:{requested:'自动选择',attempts:[{name:'美国 <节点>',reason:'Google STUN 未响应'}]}};
 w.fetch=async(path,opt={})=>{
  if(path.endsWith('/cancel'))cancelled=true;
  if(path==='/api/action')return {ok:true,json:async()=>({job:'j1',cancellable:true})};
  if(path.startsWith('/api/jobs/')){
   if(mode==='done')g.last_action=finished;
   return {ok:true,json:async()=>({status:mode,message:'正在验证 4/77：美国节点；已检查3个',result:mode==='done'?finished:null})};
  }
  return {ok:true,json:async()=>path==='/api/state'?state:{csrf:'t'}};
 };
 const timer=w.setTimeout.bind(w);w.setTimeout=(fn,ms)=>timer(fn,ms===1000?5:ms);
 w.eval(fs.readFileSync('web/app.js','utf8'));
 await until(()=>d.querySelector('[data-switch="2"]'));
 d.querySelector('[data-switch="2"]').click();d.getElementById('dialog-form').dispatchEvent(new w.Event('submit',{cancelable:true}));
 await until(()=>d.querySelector('.group-feedback')?.textContent.includes('4/77'));
 assert.match(d.querySelector('.group-feedback').textContent,/正在处理本组操作/);
 assert.doesNotMatch(d.querySelector('.group-feedback').textContent,/已生效/);
 d.querySelector('[data-cancel-test]').click();await until(()=>cancelled);
 mode='done';await until(()=>d.querySelector('.group-feedback')?.textContent.includes('未应用新节点'));
 assert.match(d.querySelector('.group-feedback').textContent,/Google STUN 未响应/);
 assert.equal(d.querySelector('.group-feedback li strong').textContent,'美国 <节点>');
 assert.equal(d.querySelector('.group-feedback').querySelector('节点'),null);
 d.getElementById('clear-operations').click();
 assert.match(d.querySelector('.group-feedback').textContent,/已检查全部77个候选/);
 assert.doesNotMatch(d.querySelector('.group-feedback').textContent,/处理完成：可用/);
 dom.window.close();console.log('PASS group feedback: inline live progress beyond three, cooperative stop, not-applied outcome, escaped reasons, persisted result after clearing jobs');
})().catch(e=>{console.error(e);process.exit(1)});
