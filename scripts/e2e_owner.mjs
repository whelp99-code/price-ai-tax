// Actual owner browser acceptance against isolated source files, API and worker.
// No credentials or real company data are read; all child processes belong to this run.
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import {spawn, execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = path.join(root, 'evidence/version-loop/V01'); fs.mkdirSync(out,{recursive:true});
fs.mkdirSync(path.join(root,'.data'),{recursive:true});
const temp = fs.mkdtempSync(path.join(root,'.data/owner-browser-'));
const report = {started_at:new Date().toISOString(), data:'synthetic source-format fixtures, not institution-verified originals', checks:[], screenshots:[], real_filing:false, policy_modified:false, sandbox_disabled:false};
const children=[];let ws,evaluate,cdp;let sequence=0;
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const freePort=()=>new Promise((resolve,reject)=>{const s=net.createServer();s.on('error',reject);s.listen(0,'127.0.0.1',()=>{const port=s.address().port;s.close(()=>resolve(port));});});
const q=JSON.stringify;
function check(name,condition=true){if(!condition)throw new Error(name);report.checks.push({name,status:'PASS'});}
async function wait(fn,label,timeout=20000){const end=Date.now()+timeout;let last;while(Date.now()<end){try{if(await fn())return;}catch(e){last=e;}await pause(100);}throw new Error('Timeout: '+label+(last?' '+last.message:''));}
async function stop(child){if(child.exitCode!==null||child.signalCode!==null)return;child.kill('SIGTERM');await wait(()=>child.exitCode!==null||child.signalCode!==null,'own process stop',10000);}
try{
 const db=path.join(temp,'source.sqlite3');
 execFileSync('python3',['-m','price','--db',db,'init','--demo'],{cwd:root,stdio:'ignore'});
 const apiPort=await freePort(),chromePort=await freePort(),url='http://127.0.0.1:'+apiPort;
 const startApi=()=>{const log=fs.openSync(path.join(out,'api-'+(++sequence)+'.log'),'w');const child=spawn('python3',['-m','price','--db',db,'serve','--port',String(apiPort)],{cwd:root,stdio:['ignore',log,log]});children.push(child);return child;};
 let api=startApi();await wait(async()=>(await fetch(url+'/health')).ok,'API ready');
 const chromeLog=fs.openSync(path.join(out,'chrome.log'),'w');
 const chrome=spawn('/usr/bin/google-chrome',['--headless=new','--disable-background-networking','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--user-data-dir='+path.join(temp,'profile'),'--remote-debugging-address=127.0.0.1','--remote-debugging-port='+chromePort,'about:blank'],{cwd:root,stdio:['ignore',chromeLog,chromeLog]});children.push(chrome);
 await wait(async()=>{if(chrome.exitCode!==null)throw new Error('Chrome exited');return (await fetch('http://127.0.0.1:'+chromePort+'/json/version')).ok;},'Chrome ready');
 const target=await(await fetch('http://127.0.0.1:'+chromePort+'/json/new?about:blank',{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
 const pending=new Map(),exceptions=[];let id=0;
 ws.addEventListener('message',event=>{const message=JSON.parse(event.data);if(message.id){const call=pending.get(message.id);if(call){pending.delete(message.id);clearTimeout(call.timer);message.error?call.reject(new Error(JSON.stringify(message.error))):call.resolve(message.result);}}else if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);});
 cdp=(method,params={})=>new Promise((resolve,reject)=>{const key=++id,timer=setTimeout(()=>{pending.delete(key);reject(new Error('CDP timeout '+method));},15000);pending.set(key,{resolve,reject,timer});ws.send(JSON.stringify({id:key,method,params}));});
 evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;};
 const click=async selector=>{await wait(()=>evaluate('!!document.querySelector('+q(selector)+')'),'element '+selector);const point=await evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});};
 const fill=(selector,value)=>evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.value=${q(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
 const selectFiles=async names=>{const document=await cdp('DOM.getDocument');const node=await cdp('DOM.querySelector',{nodeId:document.root.nodeId,selector:'#owner-files'});await cdp('DOM.setFileInputFiles',{nodeId:node.nodeId,files:names});};
 const screen=async name=>{const r=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,name),Buffer.from(r.data,'base64'));report.screenshots.push('evidence/version-loop/V01/'+name);};
 const home=()=>evaluate("fetch('/api/companies/'+document.querySelector('#owner-company').value+'/owner/home').then(r=>r.json())");
 await cdp('Runtime.enable');await cdp('Page.enable');await cdp('DOM.enable');
 await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await cdp('Page.navigate',{url});await wait(()=>evaluate('!!document.querySelector("#owner-login-form")'),'owner login');
 await fill('#owner-login-form [name="username"]','demo-owner');await fill('#owner-login-form [name="password"]','DemoOnly-Price-2026!');await click('#owner-login-form button[type="submit"]');
 await wait(()=>evaluate('!document.querySelector("#owner-workspace").hidden && document.querySelector("#owner-title").textContent.includes("자료")'),'owner workspace');
 check('owner has exactly three normal workflow tabs',await evaluate('document.querySelectorAll("[data-view]").length===3'));
 check('normal workflow contains no journal-entry or reviewer-switch form',await evaluate('!document.querySelector("[name=tax_category],[name=account],[name=deductible]")'));
 await screen('owner-empty-desktop.png');
 const bank=path.join(temp,'통장내역.csv'),invoice=path.join(temp,'세금계산서.csv'),card=path.join(temp,'카드내역.csv'),bad=path.join(temp,'손상된.pdf');
 fs.writeFileSync(bank,'합성 검증 통장\n계좌번호: 123-456-789012\n통화: KRW\n거래일시,거래내용,입금액,출금액,거래후잔액\n2026-09-01 10:00:00,용역대금,1100,0,1100\n2026-09-02 10:00:00,장비지급,0,220,880\n합계,,1100,220,\n');
 fs.writeFileSync(invoice,'작성일자,승인번호,공급자사업자등록번호,공급받는자사업자등록번호,공급가액(원),세액,합계금액\n2026-09-01,SYNTHETIC-01,111-11-11111,222-22-22222,1000,100,1100\n');
 fs.writeFileSync(card,'승인일자,카드번호,가맹점명,승인금액,승인번호,승인취소구분,통화\n2026-09-01,1111-****-****-1111,<img src=x onerror=window.__SOURCE_XSS=1>,1100,SYNTHETIC-01,승인,KRW\n2026-09-02,1111-****-****-1111,시험상점,-1100,SYNTHETIC-02,취소,KRW\n');
 fs.writeFileSync(bad,'SYNTHETIC INVALID PDF; no active content');
 await selectFiles([bank,card,invoice,bad]);
 await wait(()=>evaluate('document.querySelector("#transfer-label").textContent==="자료 접수 완료"'),'multi-file upload sealed');
 let first=await home();check('multiple source files are stored through the real picker',first.counts.documents===4);
 check('without worker sources stay queued, never called accounting complete',first.counts.reading===3&&first.accounting_complete===false);
 check('invalid file visibly remains unresolved',first.counts.needs_attention===1);
 // API restart before the reader starts: persisted outbox, blobs and session must survive.
 await stop(api);api=startApi();await wait(async()=>(await fetch(url+'/health')).ok,'API after restart');
 const workerLog=fs.openSync(path.join(out,'worker.log'),'w');
 const worker=spawn('python3',['-m','price.worker','--db',db],{cwd:root,stdio:['ignore',workerLog,workerLog]});children.push(worker);
 await wait(async()=>{const h=await home();return h.counts.read===3&&h.counts.source_cases===5;},'durable reader processes after restart');
 check('restart resumes all three durable source jobs without manual input');
 await click('#refresh-documents');await wait(()=>evaluate('document.querySelector("#document-list").textContent.includes("자료 읽기 완료")'),'actual reading status');
 await screen('owner-upload-desktop.png');
 const posted=(await home()).counts.posted_journals;
 await selectFiles([invoice,card,bank,bad]);await wait(()=>evaluate('document.querySelector("#transfer-label").textContent==="자료 접수 완료"'),'reordered reupload');
 await pause(300);const duplicate=await home();
 check('reordered identical upload reuses documents and cases',duplicate.counts.documents===4&&duplicate.counts.source_cases===5);
 check('source ingestion does not fabricate accounting completion',duplicate.accounting_complete===false&&duplicate.counts.posted_journals===posted);
 const cardDoc=duplicate.documents.find(d=>d.filename==='카드내역.csv');
 await click(`[data-document="${cardDoc.id}"] button`);await wait(()=>evaluate('document.querySelector("#owner-dialog-content table")!==null'),'extraction details');
 check('untrusted source text is inert in actual browser',await evaluate('!window.__SOURCE_XSS && !document.querySelector("#owner-dialog-content img") && document.querySelector("#owner-dialog-content").textContent.includes("<img")'));
 await click('#close-owner-dialog');
 await click('[data-view="questions"]');await wait(()=>evaluate('!document.querySelector("#view-questions").hidden'),'factual questions view');
 await click('#owner-questions .primary');await wait(()=>evaluate('!!document.querySelector("#company-facts-form")'),'company facts dialog');
 await click('#company-facts-form [name="confirmed"]');await click('#company-facts-form button[type="submit"]');
 await wait(()=>evaluate('!document.querySelector("#owner-dialog").open'),'facts saved');
 check('owner confirms facts without accounting selection or account switching',!(await home()).needs_company_confirmation);
 await click('[data-view="results"]');await wait(()=>evaluate('!document.querySelector("#view-results").hidden'),'results visible');
 check('reading result and financial result remain distinguished',await evaluate('document.querySelector("#result-notice").textContent.includes("완료를 뜻하지")'));
 await screen('owner-results-desktop.png');
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 for(const view of ['upload','questions','results']){await click(`[data-view="${view}"]`);check('390px owner view no horizontal overflow: '+view,await evaluate('document.documentElement.scrollWidth<=window.innerWidth+1'));}
 await screen('owner-mobile.png');
 check('no uncaught JavaScript errors',exceptions.length===0);report.javascript_exceptions=exceptions;
 check('owner stayed in same account',await evaluate('fetch("/api/session").then(r=>r.json()).then(s=>s.username==="demo-owner")'));
 report.status='PASS';report.limitations=['Only synthetic generic source-format files','No real institution-format acceptance','No automatic bookkeeping/tax filing claimed'];
}catch(error){report.status='FAIL';report.error=String(error.message||error);if(evaluate){try{report.page_text=String(await evaluate('document.body?.innerText')).slice(0,3000);if(/ERR_BLOCKED_BY_ADMINISTRATOR|blocked by your administrator|관리자.*차단/i.test(report.page_text))report.status='BLOCKED_POLICY';}catch{}}process.exitCode=1;
}finally{
 if(ws)ws.close();for(const child of children.reverse()){try{await stop(child);}catch{}}
 report.own_processes_stopped=children.every(child=>child.exitCode!==null||child.signalCode!==null);
 report.finished_at=new Date().toISOString();fs.writeFileSync(path.join(out,'owner-browser-report.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify({status:report.status,checks:report.checks.length,error:report.error,screenshots:report.screenshots,report:'evidence/version-loop/V01/owner-browser-report.json'}));
}
