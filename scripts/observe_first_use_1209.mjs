// Isolated synthetic first-use screen observation. No real company data.
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import {spawn, execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = path.join(root, 'evidence/first-use-1209');
fs.mkdirSync(out, {recursive: true});
const temp = fs.mkdtempSync(path.join(root, '.data/first-use-1209-'));
const db = path.join(temp, 'source.sqlite3');
const password = 'Test-Only-Price-2026!';
const bare = '거래일시,거래내용,입금액,출금액,거래후잔액,거래번호\n2026-09-03 10:00:00,용역대금,1100,0,1100,SYN-BARE-1\n';
const children = [];
let ws;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const freePort = () => new Promise((resolve, reject) => {
  const server = net.createServer();
  server.on('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const port = server.address().port;
    server.close(() => resolve(port));
  });
});
const q = JSON.stringify;
function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  child.kill('SIGTERM');
  return new Promise(resolve => child.once('exit', () => resolve()));
}
async function wait(fn, label, timeout = 20000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try { if (await fn()) return; } catch (error) { last = error; }
    await pause(100);
  }
  throw new Error('Timeout: ' + label + (last ? ' ' + last.message : ''));
}

const observation = {data: 'synthetic', screen: {}, db: {}};
try {
  execFileSync('python3', ['-c', `
from price.db import Database
from price.service import Service
from price.models import CompanyInput
db = Database(${q(db)})
Service(db).auth.bootstrap(CompanyInput(name='합성 최초사용'), 'synth-owner', ${q(password)})
`], {cwd: root, stdio: 'inherit'});
  const apiPort = await freePort();
  const chromePort = await freePort();
  const url = 'http://127.0.0.1:' + apiPort;
  const apiLog = fs.openSync(path.join(temp, 'api.log'), 'w');
  const api = spawn('python3', ['-m', 'price', '--db', db, 'serve', '--host', '127.0.0.1', '--port', String(apiPort)], {cwd: root, stdio: ['ignore', apiLog, apiLog]});
  children.push(api);
  await wait(async () => (await fetch(url + '/health')).ok, 'API ready');
  const chromeLog = fs.openSync(path.join(temp, 'chrome.log'), 'w');
  const chrome = spawn('/usr/bin/google-chrome-stable', ['--headless=new', '--disable-background-networking', '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check', '--user-data-dir=' + path.join(temp, 'profile'), '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + chromePort, 'about:blank'], {cwd: root, stdio: ['ignore', chromeLog, chromeLog]});
  children.push(chrome);
  await wait(async () => {
    if (chrome.exitCode !== null) throw new Error('Chrome exited');
    return (await fetch('http://127.0.0.1:' + chromePort + '/json/version')).ok;
  }, 'Chrome ready');
  const target = await (await fetch('http://127.0.0.1:' + chromePort + '/json/new?about:blank', {method: 'PUT'})).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, {once: true});
    ws.addEventListener('error', reject, {once: true});
  });
  const pending = new Map();
  let id = 0;
  ws.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!message.id) return;
    const call = pending.get(message.id);
    if (!call) return;
    pending.delete(message.id);
    clearTimeout(call.timer);
    message.error ? call.reject(new Error(JSON.stringify(message.error))) : call.resolve(message.result);
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const key = ++id;
    const timer = setTimeout(() => { pending.delete(key); reject(new Error('CDP timeout ' + method)); }, 20000);
    pending.set(key, {resolve, reject, timer});
    ws.send(JSON.stringify({id: key, method, params}));
  });
  const evaluate = async expression => {
    const result = await cdp('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  const click = async selector => {
    await wait(() => evaluate('!!document.querySelector(' + q(selector) + ')'), 'element ' + selector);
    const point = await evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);
    await cdp('Input.dispatchMouseEvent', {type: 'mousePressed', button: 'left', clickCount: 1, ...point});
    await cdp('Input.dispatchMouseEvent', {type: 'mouseReleased', button: 'left', clickCount: 1, ...point});
  };
  const fill = (selector, value) => evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.value=${q(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await cdp('Runtime.enable');
  await cdp('Page.enable');
  await cdp('DOM.enable');
  await cdp('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await cdp('Page.navigate', {url});
  await wait(() => evaluate('!!document.querySelector("#owner-login-form")'), 'login form');
  await fill('#owner-login-form [name="username"]', 'synth-owner');
  await fill('#owner-login-form [name="password"]', password);
  await click('#owner-login-form button[type="submit"]');
  await wait(() => evaluate('!document.querySelector("#owner-workspace").hidden'), 'workspace');
  const csv = path.join(temp, 'bare.csv');
  fs.writeFileSync(csv, bare);
  const documentNode = await cdp('DOM.getDocument');
  const input = await cdp('DOM.querySelector', {nodeId: documentNode.root.nodeId, selector: '#owner-files'});
  await cdp('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [csv]});
  await wait(() => evaluate('document.querySelector("#transfer-label") && document.querySelector("#transfer-label").textContent.includes("접수 완료")'), 'upload sealed');
  await click('[data-view="questions"]');
  await wait(() => evaluate('!!document.querySelector("#owner-questions button")'), 'account fact prompt');
  observation.screen.before_account_fact = await evaluate('document.querySelector("#owner-questions").innerText');
  await click('#owner-questions button');
  await wait(() => evaluate('!!document.querySelector("#owner-account-identifier")'), 'account fact fields');
  await fill('#owner-account-identifier', '123456789012');
  await fill('#owner-account-currency', 'KRW');
  await click('#company-facts-form [name="confirmed"]');
  await click('#company-facts-form button[type="submit"]');
  await wait(() => evaluate('!document.querySelector("#owner-dialog").open'), 'facts dialog closed');
  await wait(() => evaluate('!![...document.querySelectorAll("#owner-questions button")].find(node => node.textContent.includes("이 문서의 계좌"))'), 'document confirmation prompt');
  observation.screen.after_account_fact = await evaluate('document.querySelector("#owner-questions").innerText');
  await click('#owner-questions button.primary');
  await wait(() => evaluate('!!document.querySelector("#owner-dialog-content select")'), 'document account select');
  await evaluate(`(()=>{const select=document.querySelector("#owner-dialog-content select");select.value="123456789012";select.dispatchEvent(new Event("change",{bubbles:true}));const currency=document.querySelector("#owner-dialog-content input");currency.value="KRW";currency.dispatchEvent(new Event("input",{bubbles:true}));})()`);
  await click('#owner-dialog-content input[type="checkbox"]');
  await click('#owner-dialog-content button[type="submit"]');
  await wait(() => evaluate('document.body.innerText.includes("이 돈이 들어오거나 나간 이유는 무엇인가요")'), 'cash reason question');
  observation.screen.after_document_confirmation = await evaluate('document.querySelector("#owner-questions").innerText');
  observation.screen.shows_cash_reason = observation.screen.after_document_confirmation.includes('이 돈이 들어오거나 나간 이유는 무엇인가요');
  observation.screen.shows_customer_payment = observation.screen.after_document_confirmation.includes('고객 대금');
  const shot = await cdp('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  fs.writeFileSync(path.join(out, 'screen.png'), Buffer.from(shot.data, 'base64'));
  const dbJson = execFileSync('python3', ['-c', `
import json, sqlite3
c = sqlite3.connect(${q(db)})
c.row_factory = sqlite3.Row
facts = [json.loads(row["body"]) for row in c.execute("SELECT body FROM objects WHERE kind='company_facts'")]
records = [json.loads(row["body"]) for row in c.execute("SELECT body FROM objects WHERE kind='source_record'")]
contexts = [json.loads(row["body"]) for row in c.execute("SELECT body FROM objects WHERE kind='document_source_context'")]
blob = c.execute("SELECT content FROM blobs").fetchone()
print(json.dumps({
  "journals": c.execute("SELECT count(*) FROM objects WHERE kind='journal'").fetchone()[0],
  "instruments": facts[0]["instruments"] if facts else [],
  "source_account": records[0]["values"].get("account") if records else None,
  "source_currency": records[0]["values"].get("currency") if records else None,
  "source_gross": records[0]["values"].get("gross") if records else None,
  "blob_sha256": __import__("hashlib").sha256(blob["content"]).hexdigest() if blob else None,
  "context_applied_to_original": contexts[0].get("applied_to_original") if contexts else None,
  "context_currency_origin": contexts[0].get("currency_origin") if contexts else None,
}, ensure_ascii=False))
`], {cwd: root, encoding: 'utf8'});
  observation.db = JSON.parse(dbJson);
  observation.db.blob_matches_upload = observation.db.blob_sha256 === execFileSync('python3', ['-c', `import hashlib; print(hashlib.sha256(${q(bare)}.encode()).hexdigest())`], {encoding: 'utf8'}).trim();
  if (!observation.screen.shows_cash_reason || observation.db.journals !== 0 || observation.db.source_account !== null || observation.db.source_currency !== null || observation.db.context_applied_to_original !== false) {
    throw new Error('first-use screen or database observation failed');
  }
  observation.status = 'PASS';
} catch (error) {
  observation.status = 'FAIL';
  observation.error = String(error.message || error);
  process.exitCode = 1;
} finally {
  if (ws) ws.close();
  for (const child of children.reverse()) await stop(child);
  fs.writeFileSync(path.join(out, 'observation.json'), JSON.stringify(observation, null, 2));
}
