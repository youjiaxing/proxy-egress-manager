// Isolated DOM verification; no browser or network traffic.
const fs=require('fs'),assert=require('node:assert/strict');
const {JSDOM}=require(process.env.JSDOM_PATH||'/tmp/proxy-egress-dom/node_modules/jsdom');
const pause=()=>new Promise(r=>setTimeout(r,10));
async function until(fn){for(let i=0;i<150;i++){if(fn())return;await pause();}throw Error('UI condition timed out');}
(async()=>{
 const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 const state={health:{scheduler_valid:true},groups:[{id:'1',uid:'group-uid',name:'组',node:'a',endpoint:'local:1081',paused:true,pinned:false,lease_valid:false,tcp_ips:[],udp_ips:[],scope:{mode:'all',ids:[]}}],sources:[{slot:1,uid:'source',name:'订阅',enabled:true}],nodes:{a:{id:'a',ref:'source/a',name:'节点',source:1,disabled:false,priority:100}},settings:{}};
 const calls=[];
 w.fetch=async(path,opt={})=>{
  calls.push({path,method:opt.method||'GET'});
  let body=path==='/api/state'?state:{csrf:'fixture'};
  if(path.startsWith('/api/traffic'))body={rows:[{owner:'group-uid',node_ref:'source/a',origin:'business',period:'all',upload:10,download:20,total:30,group:'<img src=x>',node:'节点',source:'订阅'}],summary:{all:{upload:1000,download:2000,total:3000}},day:'d:2026-11-07',month:'m:2026-11',started:1,saved:2,gap:true,error:null,row_count:101,offset:path.includes('offset=100')?100:0};
  return {ok:true,json:async()=>body};
 };
 w.eval(fs.readFileSync('web/app.js','utf8'));
 await until(()=>d.querySelector('[data-traffic-group]'));
 assert.equal(calls.filter(c=>c.path.startsWith('/api/traffic')).length,0);
 d.querySelector('[data-traffic-group]').click();await until(()=>d.getElementById('traffic-refresh'));
 assert(calls.some(c=>c.path==='/api/traffic?group=group-uid'));
 assert.match(d.getElementById('traffic-body').textContent,/2.93 KiB/);
 assert.match(d.getElementById('traffic-body').textContent,/记录可能不完整/);
 assert.equal(d.querySelector('#traffic-body img'),null);
 d.getElementById('traffic-next').click();await until(()=>calls.some(c=>c.path.includes('offset=100')));
 d.getElementById('traffic-close').click();assert(!d.getElementById('traffic-dialog').open);
 d.querySelector('[data-traffic-node]').click();await until(()=>calls.some(c=>c.path==='/api/traffic?node=source%2Fa'));
 d.getElementById('logout').click();await until(()=>d.getElementById('dashboard').hidden);
 assert(!d.getElementById('traffic-dialog').open);
 assert(!calls.some(c=>c.path==='/api/tests'||c.path==='/api/action'));
 dom.window.close();console.log('PASS traffic UI: on-demand only, group/node UID, complete summaries independent of pagination, escaped labels, gap warning, logout');
})().catch(e=>{console.error(e);process.exit(1);});
