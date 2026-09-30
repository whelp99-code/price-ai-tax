// Browser acceptance through Chrome's own DevTools protocol. No packages/downloads.
// Only isolated synthetic DB/profile and child processes created by this script are used.
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import {spawn,execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'evidence/ubuntu');fs.mkdirSync(out,{recursive:true});
const temp=fs.mkdtempSync(path.join(root,'.data/browser-'));
const report={started_at:new Date().toISOString(),runtime:'Ubuntu Chrome native CDP',root,data:'synthetic only',checks:[],screenshots:[],policy_modified:false,sandbox_disabled:false};
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const port=()=>new Promise((resolve,reject)=>{const s=net.createServer();s.on('error',reject);s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
const children=[];let ws;let evaluate;
function check(name,value=true){if(!value)throw new Error(name);report.checks.push({name,status:'PASS'});}
async function wait(fn,label,ms=12000){const end=Date.now()+ms;let last;while(Date.now()<end){try{if(await fn())return;}catch(e){last=e;}await pause(100);}throw new Error('Timeout: '+label+(last?' '+last.message:''));}
try{
 const db=path.join(temp,'test.sqlite3');execFileSync('python3',['-m','price','--db',db,'init','--demo'],{cwd:root,stdio:'ignore'});
 const apiPort=await port(),chromePort=await port(),url=`http://127.0.0.1:${apiPort}`;
 const log=fs.openSync(path.join(out,'browser-server.log'),'w');
 const server=spawn('python3',['-m','price','--db',db,'serve','--port',String(apiPort)],{cwd:root,stdio:['ignore',log,log]});children.push(server);
 await wait(async()=>{if(server.exitCode!==null)throw new Error('API exited');return (await fetch(url+'/health')).ok;},'API health');
 const browserLog=fs.openSync(path.join(out,'browser-chrome.log'),'w');
 const chrome=spawn('/usr/bin/google-chrome',['--headless=new','--disable-background-networking','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--user-data-dir='+path.join(temp,'profile'),'--remote-debugging-address=127.0.0.1','--remote-debugging-port='+chromePort,'about:blank'],{cwd:root,stdio:['ignore',browserLog,browserLog]});children.push(chrome);
 await wait(async()=>{if(chrome.exitCode!==null)throw new Error('Chrome exited');return (await fetch(`http://127.0.0.1:${chromePort}/json/version`)).ok;},'Chrome DevTools');
 const target=await (await fetch(`http://127.0.0.1:${chromePort}/json/new?about:blank`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
 const pending=new Map();let seq=0;const exceptions=[];
 ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}}else if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params.exceptionDetails);});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout '+method));},12000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
 evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.text+' '+(r.exceptionDetails.exception?.description||''));return r.result.value;};
 const q=JSON.stringify;
 const click=async selector=>{await wait(()=>evaluate(`!!document.querySelector(${q(selector)})`),'element '+selector);const rect=await evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.scrollIntoView({block:'center',inline:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...rect,button:'left',clickCount:1});await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',...rect,button:'left',clickCount:1});};
 const fill=async(selector,value)=>evaluate(`(()=>{const e=document.querySelector(${q(selector)});if(!e)throw Error('input missing');e.value=${q(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
 const visible=selector=>evaluate(`(()=>{const e=document.querySelector(${q(selector)});return !!e && !e.hidden && e.getBoundingClientRect().height>0})()`);
 const modalSubmit=async()=>{await click('#dialog-form button[type="submit"]');};
 const closeModal=()=>click('#modal .dialog-close button');
 const login=async user=>{await wait(()=>visible('#login-screen'),'login screen');await fill('#login-form [name="username"]',user);await fill('#login-form [name="password"]','DemoOnly-Price-2026!');await click('#login-form button[type="submit"]');await wait(()=>visible('#workspace'),'authenticated workspace');await wait(()=>evaluate('!!document.querySelector("#page .card")'),'page loaded');};
 const logout=async()=>{await click('#logout-button');await wait(()=>visible('#login-screen'),'logout');};
 const nav=async name=>{await click(`#navigation a[href="#${name}"]`);await wait(()=>evaluate(`location.hash==='#${name}' && !!document.querySelector('#page .card')`),'route '+name);await pause(150);};
 const screen=async name=>{const r=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,name),Buffer.from(r.data,'base64'));report.screenshots.push('evidence/ubuntu/'+name);};
 await cdp('Runtime.enable');await cdp('Page.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await cdp('Page.navigate',{url:url+'/expert'});await wait(()=>evaluate('!!document.querySelector("#login-form")'),'expert login document');
 await login('demo-owner');check('browser login and persisted overview',await evaluate('document.querySelectorAll(".stat").length===4'));
 await screen('browser-home-desktop.png');
 for(const name of ['evidence','transactions','cases','journals','closing','tax','advice','ops']){await nav(name);check('desktop route '+name,await evaluate('!document.querySelector("#page > .error")'));}
 await nav('transactions');await click('[data-action="new-tx"]');await wait(()=>evaluate('document.querySelector("#modal").open'),'transaction dialog');
 const testId='UI-'+Date.now(),testPurpose='BROWSER-SYNTHETIC-APPROVAL';
 await fill('#dialog-form [name="external_id"]',testId);await fill('#dialog-form [name="event_date"]','2026-10-01');
 await fill('#dialog-form [name="counterparty"]','<img src=x onerror="window.__PRICE_XSS=1">');
 await fill('#dialog-form [name="net"]','1000');await fill('#dialog-form [name="tax"]','100');await fill('#dialog-form [name="tax_category"]','standard');await fill('#dialog-form [name="business_purpose"]',testPurpose);
 await click('#dialog-form input[name="evidence_ids"]');await modalSubmit();await wait(()=>evaluate('!document.querySelector("#modal").open'),'transaction saved');await wait(()=>evaluate(`document.querySelector('#page').textContent.includes(${q(testPurpose)})`),'saved transaction table');
 check('transaction form writes actual API state');check('untrusted HTML rendered inert',await evaluate('!window.__PRICE_XSS && !document.querySelector("#page img") && document.querySelector("#page").textContent.includes("<img src=x")'));
 const tx=await evaluate(`(()=>{const r=[...document.querySelectorAll('#page tr')].find(x=>x.textContent.includes(${q(testPurpose)}));return r.querySelector('[data-action="analyze"]').dataset.id;})()`);
 await click(`[data-action="analyze"][data-id="${tx}"]`);await wait(()=>evaluate('document.querySelector("#toast").textContent.includes("분석 상태")'),'analysis result');
 await nav('journals');const jid=await evaluate(`(()=>{const r=[...document.querySelectorAll('#page tr')].find(x=>x.textContent.includes(${q(testPurpose)}));return r.querySelector('[data-action="approve-journal"]').dataset.id;})()`);
 await click(`[data-action="approve-journal"][data-id="${jid}"]`);await fill('#dialog-form [name="reason"]','본인 승인 거부 검증');await modalSubmit();await wait(()=>evaluate('document.querySelector("#dialog-error").textContent.includes("SELF_APPROVAL")'),'self approval rejection');check('browser self approval blocked');await closeModal();await logout();
 await login('demo-reviewer');await nav('journals');await click(`[data-action="approve-journal"][data-id="${jid}"]`);await fill('#dialog-form [name="reason"]','독립 브라우저 검토');await modalSubmit();await wait(()=>evaluate('!document.querySelector("#modal").open'),'reviewer approval saved');check('independent reviewer approval via UI');await logout();
 await login('demo-owner');await nav('journals');await click(`[data-action="post-journal"][data-id="${jid}"]`);await fill('#dialog-form [name="reason"]','승인 후 브라우저 확정');await modalSubmit();await wait(()=>evaluate('!document.querySelector("#modal").open'),'posting saved');await wait(()=>evaluate(`!!document.querySelector('[data-action="reverse"][data-id="${jid}"]')`),'posted immutable record');check('approved journal posted via UI');await screen('browser-ledger-desktop.png');
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 for(const name of ['home','transactions','journals','closing','tax','advice','ops']){await nav(name);check('390px no page overflow '+name,await evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'));}
 await screen('browser-mobile.png');check('no uncaught JavaScript errors',exceptions.length===0);report.runtime_exceptions=exceptions;report.status='PASS';
}catch(error){report.status='FAIL';report.error=String(error.message||error);if(evaluate){try{report.page_text=String(await evaluate('document.body?.innerText')).slice(0,1800);if(/ERR_BLOCKED_BY_ADMINISTRATOR|blocked by your administrator|관리자.*차단/i.test(report.page_text))report.status='BLOCKED_POLICY';}catch{}}process.exitCode=1;
}finally{if(ws)ws.close();for(const c of children.reverse()){if(c.exitCode===null)c.kill('SIGTERM');}await pause(500);report.own_processes_stopped=children.every(c=>c.exitCode!==null||c.signalCode!==null);report.finished_at=new Date().toISOString();fs.writeFileSync(path.join(out,'browser-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({status:report.status,checks:report.checks.length,error:report.error,screenshots:report.screenshots,report:'evidence/ubuntu/browser-report.json'}));}
