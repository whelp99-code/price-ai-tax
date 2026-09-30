// Owner-only factual workflow, real browser/API/worker, synthetic isolated data.
import fs from 'node:fs';import path from 'node:path';import net from 'node:net';
import {spawn,execFileSync} from 'node:child_process';import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'evidence/version-loop/business-facts');fs.mkdirSync(out,{recursive:true});fs.mkdirSync(path.join(root,'.data'),{recursive:true});
const temp=fs.mkdtempSync(path.join(root,'.data/business-browser-'));
const report={started_at:new Date().toISOString(),data:'synthetic only',checks:[],screenshots:[],live_filing:false,policy_modified:false,sandbox_disabled:false};
const children=[];let ws,evaluate;const pause=ms=>new Promise(r=>setTimeout(r,ms));const q=JSON.stringify;
const port=()=>new Promise((resolve,reject)=>{const s=net.createServer();s.on('error',reject);s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
function check(name,result=true){if(!result)throw new Error(name);report.checks.push({name,status:'PASS'});}
async function wait(fn,label,max=18000){const end=Date.now()+max;let last;while(Date.now()<end){try{if(await fn())return;}catch(error){last=error;}await pause(120);}throw new Error('Timeout: '+label+(last?' '+last.message:''));}
try{
 const db=path.join(temp,'test.sqlite3');execFileSync('python3',['-m','price','--db',db,'init','--demo'],{cwd:root,stdio:'ignore'});
 const apiPort=await port(),chromePort=await port(),url='http://127.0.0.1:'+apiPort;
 const launch=(name,args)=>{const log=fs.openSync(path.join(out,name+'.log'),'w');const child=spawn(args[0],args.slice(1),{cwd:root,stdio:['ignore',log,log]});children.push(child);return child;};
 launch('api',['python3','-m','price','--db',db,'serve','--port',String(apiPort)]);
 await wait(async()=>(await fetch(url+'/health')).ok,'API');
 launch('worker',['python3','-m','price.worker','--db',db]);
 launch('chrome',['/usr/bin/google-chrome','--headless=new','--disable-background-networking','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--user-data-dir='+path.join(temp,'profile'),'--remote-debugging-address=127.0.0.1','--remote-debugging-port='+chromePort,'about:blank']);
 await wait(async()=>(await fetch('http://127.0.0.1:'+chromePort+'/json/version')).ok,'Chrome');
 const target=await(await fetch('http://127.0.0.1:'+chromePort+'/json/new?about:blank',{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
 let seq=0;const pending=new Map(),errors=[];
 ws.addEventListener('message',event=>{const message=JSON.parse(event.data);if(message.id){const p=pending.get(message.id);if(p){pending.delete(message.id);clearTimeout(p.timer);message.error?p.reject(new Error(JSON.stringify(message.error))):p.resolve(message.result);}}else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails);});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP '+method));},15000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
 evaluate=async expression=>{const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);return result.result.value;};
 const click=async selector=>{await wait(()=>evaluate('!!document.querySelector('+q(selector)+')'),'element '+selector);const point=await evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});};
 const fill=(selector,value)=>evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.value=${q(value)};e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
 const api=(suffix)=>evaluate(`fetch('/api/companies/'+document.querySelector('#owner-company').value+${q(suffix)}).then(r=>r.json())`);
 const screenshot=async name=>{const data=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,name),Buffer.from(data.data,'base64'));report.screenshots.push('evidence/version-loop/business-facts/'+name);};
 await cdp('Runtime.enable');await cdp('Page.enable');await cdp('DOM.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await cdp('Page.navigate',{url});await wait(()=>evaluate('!!document.querySelector("#owner-login-form")'),'login form');
 await fill('#owner-login-form [name=username]','demo-owner');await fill('#owner-login-form [name=password]','DemoOnly-Price-2026!');await click('#owner-login-form button');
 await wait(()=>evaluate('!document.querySelector("#owner-workspace").hidden'),'workspace');
 const originals={
  '사업자등록증.txt':'사업자등록증\n법인명(단체명): 합성 검증회사\n등록번호: 222-22-22222\nSYNTHETIC ONLY\n',
  '매입세금계산서.csv':'작성일자,승인번호,공급자사업자등록번호,공급받는자사업자등록번호,공급가액(원),세액,합계금액\n2026-09-01,FACT-INV-01,111-11-11111,222-22-22222,1000,100,1100\n',
  '통장내역.csv':'계좌번호: 123456789012\n통화: KRW\n거래일자,거래내용,입금액,출금액,거래번호,상대방사업자등록번호,청구참조번호\n2026-09-01,공급사 대금,0,1100,BANK-FACT-01,111-11-11111,FACT-INV-01\n'
 };
 const files=Object.entries(originals).map(([name,data])=>{const file=path.join(temp,name);fs.writeFileSync(file,data);return file;});
 const dom=await cdp('DOM.getDocument');const node=await cdp('DOM.querySelector',{nodeId:dom.root.nodeId,selector:'#owner-files'});await cdp('DOM.setFileInputFiles',{nodeId:node.nodeId,files});
 await wait(async()=>(await api('/owner/home')).counts.read===3,'automatic read');check('originals upload without mapping or manual transactions');
 await click('[data-view=questions]');await wait(()=>evaluate('!!document.querySelector("#owner-questions .primary")'),'company confirmation button');await click('#owner-questions .primary');
 await wait(()=>evaluate('!!document.querySelector("#company-facts-form")'),'company facts');
 check('company identity automatically read from document',await evaluate('document.querySelector("#company-facts-form [name=business_number]").value==="2222222222"'));
 await evaluate('document.querySelectorAll("#company-facts-form input[type=checkbox]").forEach(x=>{x.checked=true;x.dispatchEvent(new Event("change",{bubbles:true}))})');
 await click('#company-facts-form button[type=submit]');await wait(()=>evaluate('!document.querySelector("#owner-dialog").open'),'company facts saved');
 await wait(async()=>{const data=await api('/owner/events');return data.events.some(e=>e.direction==='purchase'&&e.kind==='invoice');},'economic invoice');
 await wait(async()=>(await api('/event-settlements')).allocations.some(a=>a.current&&a.automatic),'automatic reference-linked payment');
 check('explicit invoice reference and party link purchase to payment automatically');
 await wait(()=>evaluate('!!document.querySelector("#event-fact-questions [data-question$=\\\":business_use\\\"]")'),'plain-language question');
 const invoice=(await api('/owner/events')).events.find(e=>e.kind==='invoice');
 const businessSelector='#event-fact-questions [data-question="'+invoice.id+':business_use"] button';
 await click(businessSelector); // first documented choice: company business
 await wait(()=>evaluate(`!!document.querySelector('[data-question="${invoice.id}:use_type"]')`),'purpose question after answer');
 await click('#event-fact-questions [data-question="'+invoice.id+':use_type"] button');
 await wait(async()=>{const e=(await api('/owner/events')).events.find(e=>e.id===invoice.id);return e.facts.business_use==='business'&&e.facts.use_type==='operations';},'facts saved');
 check('answering factual questions advances workflow without accounts or tax inputs');
 const questions=await api('/owner/questions');check('bank purpose is not redundantly asked after source-proven settlement',questions.questions.length===0);
 const events=await api('/owner/events');check('no false posting completion',events.events.every(e=>e.accounting_complete===false));
 check('facts retain source or owner basis',events.events.find(e=>e.kind==='bank').facts_from_sources.includes('cash_reason'));
 await click('[data-view=results]');await screenshot('facts-results-desktop.png');
 check('runtime policy posting still not silently enabled',(await api('/policies')).execution_enabled===false);
 check('release gate does not invent V10 acceptance',(await api('/ops/release-readiness')).final_accepted===false);
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 for(const view of ['upload','questions','results']){await click('[data-view='+view+']');check('mobile overflow '+view,await evaluate('document.documentElement.scrollWidth<=window.innerWidth+1'));}
 await screenshot('facts-mobile.png');check('same owner account throughout',await evaluate('fetch("/api/session").then(r=>r.json()).then(x=>x.username==="demo-owner")'));
 check('no uncaught JavaScript errors',errors.length===0);report.status='PASS';
}catch(error){report.status='FAIL';report.error=String(error.message||error);if(evaluate){try{report.page_text=String(await evaluate('document.body.innerText')).slice(0,2500);if(/ERR_BLOCKED_BY_ADMINISTRATOR|blocked by your administrator/i.test(report.page_text))report.status='BLOCKED_POLICY';}catch{}}process.exitCode=1;
}finally{if(ws)ws.close();for(const child of children.reverse()){if(child.exitCode===null&&child.signalCode===null)child.kill('SIGTERM');}await pause(600);report.own_processes_stopped=children.every(c=>c.exitCode!==null||c.signalCode!==null);report.finished_at=new Date().toISOString();fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({status:report.status,checks:report.checks.length,error:report.error,report:'evidence/version-loop/business-facts/report.json',screenshots:report.screenshots}));}
