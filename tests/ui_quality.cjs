// Isolated UI/API simulation, not a real network or screenshot test.
const fs=require('fs'),assert=require('node:assert/strict');
const {JSDOM}=require(process.env.JSDOM_PATH||'/tmp/proxy-egress-dom/node_modules/jsdom');
const pause=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let i=0;i<150;i++){if(fn())return;await pause();}throw Error('UI condition timed out');}
(async()=>{
 const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 w.confirm=()=>true;
 const a={id:'a',ref:'s/a',name:'美国 A',source:1,disabled:false,priority:100,last_probe:null},b={...a,id:'b',ref:'t/b',name:'美国 B',source:10};
 const group={id:'1',uid:'g1',name:'业务 A',node:'a',endpoint:'proxy-egress-manager:1081',paused:false,pinned:false,lease_valid:false,tcp_ips:[],udp_ips:[],scope:{mode:'nodes',ids:['s/a','t/b']},candidate_refs:['s/a','t/b'],candidate_count:2,reason:'没有合格节点'};
 const state={health:{scheduler_valid:true},groups:[group],sources:[{slot:1,uid:'s',name:'订阅一',enabled:true},{slot:10,uid:'t',name:'订阅十',enabled:true}],nodes:{a,b},settings:{}};
 const posts=[];
 w.fetch=async(path,opt={})=>{
  if(opt.method==='POST')posts.push({path,body:JSON.parse(opt.body)});
  const body=path==='/api/state'?state:path.startsWith('/api/quality')?{records:[],series:[],coverage_start:null}:path.startsWith('/api/action')||path==='/api/tests'?{job:'job1'}:path.startsWith('/api/jobs')?{status:'done',message:'操作已完成'}:{csrf:'test'};
  return {ok:true,json:async()=>body};
 };
 const native=w.setTimeout.bind(w);w.setTimeout=(fn,ms)=>native(fn,ms===1000?5:ms);
 w.eval(fs.readFileSync('web/app.js','utf8'));
 await until(()=>d.querySelector('[data-view-source="1"]'));
 d.querySelector('[data-view-source="1"]').click();
 assert.equal(d.getElementById('source-filter').value,'1');assert.equal(d.querySelectorAll('#node-cards article').length,1);
 d.querySelector('[data-layout=table]').click();await until(()=>!d.getElementById('node-table').hidden);
 assert.equal(w.localStorage.getItem('pem-layout'),'table');assert.equal(d.querySelectorAll('#node-list tr').length,1);
 d.querySelector('[data-quality-node="s/a"]').click();await until(()=>d.getElementById('quality-udp'));
 assert.match(d.getElementById('quality-body').textContent,/尚无.*UDP/);
 d.getElementById('quality-udp').click();await until(()=>posts.some(p=>p.path==='/api/tests'));
 assert.deepEqual(posts.find(p=>p.path==='/api/tests').body.nodes,['s/a']);
 d.getElementById('quality-close').click();assert.equal(d.getElementById('quality-dialog').open,false);
 await until(()=>d.getElementById('operation-list').textContent.includes('处理完成'));
 d.querySelector('[data-scope="1"]').click();assert.equal(d.getElementById('dialog').open,true);
 const mode=d.getElementById('scope-mode');mode.value='sources';mode.dispatchEvent(new w.Event('change'));
 d.querySelector('#scope-options input[value=t]').checked=true;
 d.querySelector('[name=confirm_disconnect]').checked=true;
 d.getElementById('dialog-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 await until(()=>posts.some(p=>p.body.kind==='group_scope'));
 assert.deepEqual(posts.find(p=>p.body.kind==='group_scope').body,{kind:'group_scope',group:'1',scope:{mode:'sources',ids:['t']},confirm_disconnect:true});
 d.getElementById('logout').click();await until(()=>d.getElementById('dashboard').hidden);
 assert.equal(d.getElementById('quality-dialog').open,false);
 dom.window.close();console.log('PASS quality UI: exact source filter, remembered table layout, independent UDP request, missing data, scope checkbox arrays, disconnect consent, logout cleanup');
})().catch(e=>{console.error(e);process.exit(1);});
