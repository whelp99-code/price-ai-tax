// Actual browser test: owner uploads originals, separate reviewer verifies,
// owner sees real ledger results without changing role or entering accounts.
import fs from 'node:fs';import path from 'node:path';import net from 'node:net';
import {spawn,execFileSync} from 'node:child_process';import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'evidence/version-loop/real-bookkeeping');fs.mkdirSync(out,{recursive:true});
fs.mkdirSync(path.join(root,'.data'),{recursive:true});const temp=fs.mkdtempSync(path.join(root,'.data/books-browser-'));
const children=[],browsers=[];const report={started_at:new Date().toISOString(),checks:[],screenshots:[],synthetic_only:true,live_bank_transfers:false,live_filings:false,sandbox_disabled:false};
const pause=ms=>new Promise(r=>setTimeout(r,ms)),q=JSON.stringify;
const port=()=>new Promise((resolve,reject)=>{const s=net.createServer();s.on('error',reject);s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
async function wait(fn,name,timeout=20000){const end=Date.now()+timeout;let err;while(Date.now()<end){try{if(await fn())return;}catch(e){err=e;}await pause(100);}throw Error('Timeout: '+name+(err?' '+err.message:''));}
function check(name,condition=true){if(!condition)throw Error(name);report.checks.push({name,status:'PASS'});}
async function startBrowser(label){const p=await port();const fd=fs.openSync(path.join(out,label+'-chrome.log'),'w');const proc=spawn('/usr/bin/google-chrome',['--headless=new','--disable-background-networking','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--user-data-dir='+path.join(temp,label+'-profile'),'--remote-debugging-address=127.0.0.1','--remote-debugging-port='+p,'about:blank'],{cwd:root,stdio:['ignore',fd,fd]});children.push(proc);await wait(async()=>{if(proc.exitCode!==null)throw Error('Chrome exit');return (await fetch('http://127.0.0.1:'+p+'/json/version')).ok;},'Chrome '+label);
 const target=await(await fetch('http://127.0.0.1:'+p+'/json/new?about:blank',{method:'PUT'})).json();const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true});});let id=0;const pending=new Map(),errors=[];
 ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const c=pending.get(m.id);if(c){clearTimeout(c.timer);pending.delete(m.id);m.error?c.reject(Error(JSON.stringify(m.error))):c.resolve(m.result);}}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const n=++id,timer=setTimeout(()=>{pending.delete(n);reject(Error('CDP timeout '+method));},15000);pending.set(n,{resolve,reject,timer});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;};
 const click=async selector=>{await wait(()=>evaluate(`!!document.querySelector(${q(selector)})`),'element '+selector);const point=await evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});};
 const fill=(selector,value)=>evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.value=${q(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
 const clickText=async(selector,text)=>{await wait(()=>evaluate(`[...document.querySelectorAll(${q(selector)})].some(e=>e.textContent===${q(text)})`),'button '+text);const unique='click-'+Date.now();await evaluate(`(()=>{const e=[...document.querySelectorAll(${q(selector)})].find(e=>e.textContent===${q(text)});e.id=${q(unique)}})()`);await click('#'+unique);};
 const screen=async name=>{const r=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});const filename=label+'-'+name+'.png';fs.writeFileSync(path.join(out,filename),Buffer.from(r.data,'base64'));report.screenshots.push('evidence/version-loop/real-bookkeeping/'+filename);};
 await cdp('Runtime.enable');await cdp('Page.enable');await cdp('DOM.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});const browser={ws,cdp,evaluate,click,fill,clickText,screen,errors};browsers.push(browser);return browser;
}
async function login(b,url,username){await b.cdp('Page.navigate',{url});await wait(()=>b.evaluate('!!document.querySelector("#owner-login-form")'),'login form');await b.fill('#owner-login-form [name=username]',username);await b.fill('#owner-login-form [name=password]','Synthetic-Books-Only-2026!');await b.click('#owner-login-form button[type=submit]');await wait(()=>b.evaluate('!document.querySelector("#owner-workspace").hidden'),'logged in '+username);}
let owner,reviewer,url,fixture;
try{
 fixture=JSON.parse(execFileSync('python3',['scripts/seed_bookkeeping_e2e.py','--folder',temp],{cwd:root,encoding:'utf-8'}));
 const p=await port();url='http://127.0.0.1:'+p;
 for(const [name,args] of [['api',['-m','price','--db',fixture.db,'serve','--port',String(p)]],['worker',['-m','price.worker','--db',fixture.db]]]){const fd=fs.openSync(path.join(out,name+'.log'),'w');children.push(spawn('python3',args,{cwd:root,stdio:['ignore',fd,fd]}));}
 await wait(async()=>(await fetch(url+'/health')).ok,'isolated API');owner=await startBrowser('owner');reviewer=await startBrowser('reviewer');await login(owner,url,'books-owner');await login(reviewer,url,'books-reviewer');
 check('owner and reviewer run in distinct browser profiles');
 await owner.click('[data-view=results]');await wait(()=>owner.evaluate('!!document.querySelector("#owner-money-report .stats")'),'owner initial reports');
 check('unknown opening balance is not fabricated as zero',await owner.evaluate('document.querySelector("#owner-money-report").textContent.includes("자료 확인 필요")'));
 await owner.click('[data-view=upload]');const tree=await owner.cdp('DOM.getDocument');const input=await owner.cdp('DOM.querySelector',{nodeId:tree.root.nodeId,selector:'#owner-files'});await owner.cdp('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[fixture.opening_file]});
 await wait(()=>owner.evaluate('document.querySelector("#transfer-label").textContent==="자료 접수 완료"'),'opening upload');
 const apiPath='/api/companies/'+fixture.company;
 const get=(browser,path)=>browser.evaluate(`fetch(${q(apiPath+path)}).then(r=>{if(!r.ok)throw Error('API '+r.status);return r.json()})`);
 await wait(async()=>(await get(owner,'/opening-balances')).length===1,'worker creates opening proposal');let opening=(await get(owner,'/opening-balances'))[0];
 check('original-only upload creates parsed opening proposal with no owner account mapping',opening.lines.length===2&&opening.state==='DRAFT');
 const denied=await owner.evaluate(`fetch(${q(apiPath+'/opening-balances/'+opening.id+'/approve')},{method:'POST',headers:{'content-type':'application/json','x-csrf-token':state.csrf},body:JSON.stringify({expected_revision:${opening.revision},reason:'self approval test'})}).then(r=>r.status)`);
 check('owner self-approval denied at real HTTP boundary',denied===403);
 await reviewer.cdp('Page.navigate',{url:url+'/bookkeeping'});await wait(()=>reviewer.evaluate('document.querySelector("#books-content").textContent.includes("기초잔액")'),'review workbench');await reviewer.fill('#books-reason','합성 원본·기초일·금액과 비용 인식 일정 독립 대조');
 await reviewer.clickText(`[data-record="${opening.id}"] button`,'독립 검토 승인');await wait(async()=>(await get(reviewer,'/opening-balances'))[0].state==='APPROVED','opening approved');
 await reviewer.clickText(`[data-record="${opening.id}"] button`,'검토된 기초잔액 반영');await wait(async()=>(await get(reviewer,'/opening-balances'))[0].state==='POSTED','opening posted');
 check('reviewer browser posts independently approved opening to actual ledger');
 for(const id of [fixture.prepaid_id,fixture.asset_id]){
   await reviewer.clickText(`[data-record="${id}"] button`,'일정 검토 승인');await wait(async()=>(await get(reviewer,'/book-schedules')).find(p=>p.id===id).state==='APPROVED','schedule approved');
   await reviewer.clickText(`[data-record="${id}"] button`,'도래한 일정 반영');await wait(async()=>(await get(reviewer,'/book-schedules')).find(p=>p.id===id).recognized_expense>0,'schedule posted');
 }
 const plans=await get(reviewer,'/book-schedules');
 check('prepaid actual expense and remaining value conserve 1001 won',plans.find(p=>p.id===fixture.prepaid_id).recognized_expense===674&&plans.find(p=>p.id===fixture.prepaid_id).book_value===327);
 check('asset depreciation actually posts to ledger',plans.find(p=>p.id===fixture.asset_id).recognized_expense===2000&&plans.find(p=>p.id===fixture.asset_id).book_value===1000);
 await reviewer.screen('reviewed-costs');
 await owner.click('[data-view=results]');await wait(()=>owner.evaluate('!!document.querySelector("#book-month")'),'period picker');await owner.fill('#book-month','2026-08');await owner.clickText('#book-period-controls button','결과 새로고침');
 await wait(()=>owner.evaluate('document.querySelector("#owner-money-report").textContent.includes("-670원")'),'owner actual expense results');
 const statements=await get(owner,'/owner/financial-statements?start=2026-08-01&end=2026-08-31');
 check('income statement includes period expense depreciation and COGS',statements.income_statement.income===1000&&statements.income_statement.expenses===1670&&statements.income_statement.net_profit===-670);
 check('balance sheet and cash reconcile to source-backed opening',statements.balance_sheet.difference===0&&statements.cash.opening===1000000&&statements.cash.closing===1000000&&statements.cash.reconciles);
 const stock=await get(owner,'/owner/inventory');check('owner sees exact FIFO remainder',stock.items[0].quantity===2&&stock.items[0].value===668);
 check('owner sees period costs without accounting input fields',await owner.evaluate('document.querySelector("#owner-cost-report").textContent.includes("327원")&&!document.querySelector("[name=account],[name=deductible],[name=tax_category]")'));
 check('unclosed statements remain marked provisional',statements.book_values_verified===false&&statements.blockers.some(b=>b.code==='REPORT_PERIOD_NOT_CLOSED'));
 await owner.screen('financial-results');
 // Existing authenticated closing API is used by the reviewer, never owner.
 await reviewer.evaluate(`(async()=>{let p=(await api('/periods')).find(x=>x.id===${q(fixture.period_id)});p=await api('/periods/'+p.id+'/coverage',{expected_revision:p.revision,opening_reviewed:true,sources_confirmed:['manual'],evidence_note:'합성 원본 범위와 기초잔액 독립 대조'});const closed=await api('/periods/'+p.id+'/close',{expected_revision:p.revision,reason:'기초·기간비용·재고·원장 검증'});if(!closed.closed)throw Error('not closed');})()`);
 await reviewer.click('#books-refresh');await reviewer.clickText('#books-content button','합성 7~8월 검토표 작성');await wait(async()=>(await get(reviewer,'/vat/returns')).length===1,'VAT workpaper generated');const vat=(await get(reviewer,'/vat/returns'))[0];
 check('VAT workpaper maps fixed-asset and general purchases without double tax',vat.totals.output_vat===100&&vat.totals.deductible_input_vat===400&&vat.fields.purchase_fixed_asset.tax===300&&vat.fields.purchase_general.tax===100);
 check('VAT workpaper never claims actual filing or receipt',vat.official_form===false&&vat.live_submission_ready===false);
 const report2=await get(owner,'/owner/financial-statements?start=2026-08-01&end=2026-08-31');check('closed period changes report status only after successful closing',report2.period_closed===true&&report2.balance_sheet.balanced);
 await owner.clickText('#book-period-controls button','결과 새로고침');await wait(()=>owner.evaluate('document.querySelector("#owner-vat-report").textContent.includes("예상 환급 검토금액")'),'VAT visible to owner');await owner.screen('tax-summary');
 for(const b of [owner,reviewer]){await b.cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});check((b===owner?'owner':'reviewer')+' mobile has no horizontal overflow',await b.evaluate('document.documentElement.scrollWidth<=innerWidth+1'));await b.screen('mobile');}
 check('owner stayed books-owner throughout review in separate profile',await owner.evaluate('fetch("/api/session").then(r=>r.json()).then(s=>s.username==="books-owner")'));
 check('no uncaught browser JavaScript errors',owner.errors.length===0&&reviewer.errors.length===0);
 report.status='PASS';report.limitations=['Synthetic data and declared accounting assumptions only','No institutional format acceptance or professional-license verification','No live filing or payroll payment','Not full V10 acceptance'];
}catch(error){report.status='FAIL';report.error=String(error.message||error);try{if(owner)report.owner_text=String(await owner.evaluate('document.body.innerText')).slice(0,3000);if(reviewer)report.reviewer_text=String(await reviewer.evaluate('document.body.innerText')).slice(0,3000);}catch{}process.exitCode=1;
}finally{for(const b of browsers)b.ws.close();for(const child of children.reverse()){if(child.exitCode===null&&child.signalCode===null){child.kill('SIGTERM');try{await wait(()=>child.exitCode!==null||child.signalCode!==null,'own process stop',10000);}catch{}}}report.finished_at=new Date().toISOString();report.own_processes_stopped=children.every(p=>p.exitCode!==null||p.signalCode!==null);fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({status:report.status,checks:report.checks.length,error:report.error,report:'evidence/version-loop/real-bookkeeping/report.json',screenshots:report.screenshots}));}
