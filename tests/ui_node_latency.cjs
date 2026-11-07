const fs=require('fs'),assert=require('node:assert/strict');
const {JSDOM}=require(process.env.JSDOM_PATH||'/tmp/proxy-egress-dom/node_modules/jsdom');
const pause=()=>new Promise(r=>setTimeout(r,5));
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await pause();}throw Error('node latency UI timed out');}
(async()=>{
 const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'https://example.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};w.confirm=()=>true;
 const now=Date.now()/1000;
 const a={id:'a',ref:'s/a',name:'美国 <A>',source:1,disabled:false,priority:100,last_probe:{time:now-10,samples:[{method:'http-get-headers',ok:true,ms:208}],tcp_ips:[],udp_ips:[]},last_test:{test:'http',current:true,time:now,error:'timeout',samples:[]}};
 const b={...a,id:'b',ref:'s/b',name:'美国 B',last_probe:null,last_test:null};
 const state={health:{scheduler_valid:true},groups:[],nodes:{a,b},sources:[{slot:1,uid:'s',name:'订阅一',enabled:true}],settings:{}};
 let hold=true;const posts=[];
 w.fetch=async(path,opt={})=>{
  if(path==='/api/tests'){posts.push(JSON.parse(opt.body));return {ok:true,json:async()=>({job:'j'})};}
  if(path==='/api/jobs/j')return {ok:true,json:async()=>hold?{status:'running'}:{status:'done',result:[]}};
  return {ok:true,json:async()=>path==='/api/state'?state:{csrf:'t'}};
 };
 const timer=w.setTimeout.bind(w);w.setTimeout=(fn,ms)=>timer(fn,ms===1000?5:ms);
 w.eval(fs.readFileSync('web/app.js','utf8')+'\nwindow.refreshForTest=refresh;');
 await until(()=>d.querySelector('.node-latency'));
 assert.equal(d.querySelector('.node-latency').textContent,'超时','latest failed manual test must not show older success');
 assert.equal(d.querySelector('.simple-node details').open,false);
 assert.equal(d.querySelector('.node-name A'),null,'node names escaped');
 const box=d.querySelector('#node-cards [data-select-node="s/a"]');box.checked=true;box.dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.ok(d.querySelector('.simple-node').classList.contains('is-selected'));
 d.querySelector('.node-latency').click();await until(()=>posts.length===1);
 assert.deepEqual(posts[0],{nodes:['s/a'],test:'http'});
 const bBox=d.querySelector('#node-cards [data-select-node="s/b"]');bBox.checked=true;bBox.dispatchEvent(new w.Event('change',{bubbles:true}));
 d.getElementById('test-selected').click();await pause();assert.equal(posts.length,1,'overlapping batch rejected without clearing original running marker');
 assert.equal(d.querySelector('.node-latency').textContent,'测试中…');
 assert.equal(d.querySelector('.node-latency').textContent,'测试中…');assert.equal(d.getElementById('quality-dialog').open,false);
 a.last_test={test:'http',current:true,time:now+1,samples:[{method:'http-get-headers',ok:true,ms:168}]};hold=false;
 await until(()=>d.querySelector('.node-latency').textContent==='168 ms');
 assert.ok(d.querySelector('.node-latency').classList.contains('latency-good'));
 a.last_test={test:'http',current:false,time:now+2,samples:[{method:'http-get-headers',ok:true,ms:1}]};
 await w.refreshForTest();assert.equal(d.querySelector('.node-latency').textContent,'208 ms','stale-identity manual result excluded');
 assert.equal(posts.length,1,'opening, selecting, refreshing must not probe');
 dom.window.close();console.log('PASS simple nodes: latest timeout, colored latency, collapsed detail, card multiselect, one-click test with progress, stale result excluded, no implicit probes');
})().catch(e=>{console.error(e);process.exit(1)});
