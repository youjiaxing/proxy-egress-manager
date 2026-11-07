const fs=require('fs'),assert=require('node:assert/strict');
const {JSDOM}=require(process.env.JSDOM_PATH||'/tmp/proxy-egress-dom/node_modules/jsdom');
const pause=()=>new Promise(r=>setTimeout(r,5));
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await pause();}throw Error('condition timed out');}
(async()=>{
 const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};w.confirm=()=>true;
 const nodes=Object.fromEntries(['a','b','c'].map(id=>[id,{id,ref:'s/'+id,source:1,name:'美国 '+id,disabled:false,last_probe:null}]));
 const g={id:'1',uid:'g1',name:'组1',node:null,paused:false,pinned:false,lease_valid:false,tcp_ips:[],udp_ips:[],scope:{mode:'all',ids:[]},reason:'尚无合格节点',endpoint:'proxy-egress-manager:1081'};
 const state={health:{scheduler_valid:true},groups:[g],sources:[{slot:1,uid:'s',name:'订阅一',enabled:true}],nodes,settings:{}};
 const selection={total:3,checked:3,qualified:2,recommended:['s/b','s/a'],attempts:[{node_ref:'s/a',name:'美国 a',time:200,http_ms:100,eligible:true,reason:'双协议出口已验证'},{node_ref:'s/b',name:'美国 b',time:200,http_ms:30,eligible:true,reason:'双协议出口已验证',history:{successes:100,attempts:105,variation_ms:12,target:'https://test/',origin:'running'}}]};
 const posts=[];let kind;
 w.fetch=async(path,opt={})=>{
  if(path==='/api/action'){const body=JSON.parse(opt.body);posts.push(body);kind=body.kind;return {ok:true,json:async()=>({job:'j1',cancellable:true})};}
  if(path.startsWith('/api/jobs/')){
   if(kind==='group_evaluate'){g.last_evaluation=selection;return {ok:true,json:async()=>({status:'done',result:{kind,group:'1',outcome:'evaluated',message:'评估完成，待确认',selection}})};}
   return {ok:true,json:async()=>({status:'done',result:{kind,group:'1',outcome:'saved_available',message:'节点池已保存'}})};
  }
  return {ok:true,json:async()=>path==='/api/state'?state:{csrf:'t'}};
 };
 const timer=w.setTimeout.bind(w);w.setTimeout=(fn,ms)=>timer(fn,ms===1000?5:ms);
 w.eval(fs.readFileSync('web/app.js','utf8'));
 await until(()=>d.querySelector('[data-scope="1"]'));
 d.querySelector('[data-scope="1"]').click();assert.match(d.getElementById('dialog-title').textContent,/手动/);
 assert.equal(posts.length,0);assert.match(d.getElementById('scope-options').textContent,/未评估/);
 d.getElementById('dialog-cancel').click();
 d.querySelector('[data-evaluate="1"]').click();await until(()=>d.getElementById('dialog-title').textContent.includes('自动优选'));
 assert.equal(posts[0].kind,'group_evaluate');assert.equal(posts[0].group_uid,'g1');
 assert.equal(d.querySelectorAll('#scope-options input:checked').length,2);
 assert.match(d.getElementById('scope-options').textContent,/30 ms/);assert.match(d.getElementById('scope-options').textContent,/100\/105/);
 assert.match(d.getElementById('scope-options').textContent,/尚未读取|正在读取|暂无同配置历史统计/);
 d.querySelector('#scope-options [value="s/a"]').checked=false;
 d.getElementById('dialog-form').dispatchEvent(new w.Event('submit',{cancelable:true}));
 await until(()=>posts.some(p=>p.kind==='group_scope'));
 assert.deepEqual(posts.find(p=>p.kind==='group_scope').scope,{mode:'nodes',ids:['s/b']});
 assert.ok(posts.every(p=>p.kind!=='group_switch'));
 await until(()=>d.getElementById('operation-list').textContent.includes('节点池已保存'));
 dom.window.close();console.log('PASS pool UI: manual list without probes, unknown quality, automatic evaluation, recommended multiselect with latency/history, explicit confirmation, no implicit switching');
})().catch(e=>{console.error(e);process.exit(1)});
