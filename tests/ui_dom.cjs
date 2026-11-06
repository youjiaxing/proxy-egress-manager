// DOM simulation, not a real-browser or visual-layout test.
// npm install --prefix /tmp/proxy-egress-dom jsdom@26.1.0
const assert = require('node:assert/strict');
const {JSDOM, CookieJar, VirtualConsole} = require(process.env.JSDOM_PATH || '/tmp/proxy-egress-dom/node_modules/jsdom');
const origin = process.env.UI_TEST_URL || 'http://127.0.0.1:18080';
const jar = new CookieJar();
const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', e => errors.push(e.message));
const pause = ms => new Promise(r=>setTimeout(r,ms));
async function until(fn) { for(let i=0;i<100;i++){if(fn())return;await pause(25);}throw new Error('UI condition timed out'); }
(async()=>{
  const dom = await JSDOM.fromURL(origin, {resources:'usable',runScripts:'dangerously',cookieJar:jar,virtualConsole,beforeParse(window){
    window.fetch=async(path,options={})=>{
      const response=await fetch(new URL(path,origin),{...options,headers:{...(options.headers||{}),Cookie:jar.getCookieStringSync(origin),...(options.method==='POST'?{Origin:origin}:{})}});
      for(const cookie of response.headers.getSetCookie())jar.setCookieSync(cookie,origin);
      return response;
    };
    window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
    window.HTMLDialogElement.prototype.close=function(){this.open=false;};
  }});
  const w=dom.window, d=w.document;
  await until(()=>d.readyState === 'complete');
  await pause(100);
  d.querySelector('[name=password]').value='local-preview-password-2026';
  d.getElementById('login-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  await until(()=>!d.getElementById('dashboard').hidden);
  await until(()=>d.querySelectorAll('#group-list article').length===2);
  assert.match(d.getElementById('group-list').textContent,/proxy-egress-manager:1081/);
  assert.match(d.getElementById('group-list').textContent,/已暂停/);
  for(const name of ['sources','nodes','history','settings','groups']){
    d.querySelector(`[data-tab="${name}"]`).click();
    assert.equal(d.getElementById(name).hidden,false);
  }
  d.getElementById('add-source').click();
  assert.equal(d.getElementById('dialog').open,true);
  assert.match(d.getElementById('dialog-fields').textContent,/HTTPS/);
  d.getElementById('dialog-cancel').click();
  assert.equal(d.getElementById('dialog').open,false);
  d.getElementById('logout').click();
  await until(()=>d.getElementById('dashboard').hidden);
  assert.equal(d.getElementById('login').hidden,false);
  assert.deepEqual(errors,[]);
  dom.window.close();
  console.log('UI DOM simulation: login, group cards, tabs, source dialog, logout passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
