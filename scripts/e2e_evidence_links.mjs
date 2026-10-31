// Dev-only browser E2E for the transaction evidence detail. Temporary DB, loopback port other than 4382.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {spawn, execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'price-evidence-ui-'));
const shot = process.env.EVIDENCE_UI_SHOT || path.join(temp, 'evidence-links.png');
const password = 'Synthetic-Ui-Evidence-2026!';
const children = [];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const freePort = () => new Promise((resolve, reject) => {
  const server = net.createServer();
  server.on('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const port = server.address().port;
    server.close(() => resolve(port));
  });
});

function seed(db) {
  const code = `
import base64, json
from price.db import Database
from price.models import CompanyInput
from price.service import Service
db = Database(${JSON.stringify(db)})
svc = Service(db)
company, owner = svc.auth.bootstrap(CompanyInput(name='증빙 화면 시험 법인'), 'ui-owner', ${JSON.stringify(password)})
raw = base64.b64encode(b'SYNTHETIC UI EVIDENCE - NOT REAL TAX DATA\\n').decode()
first = svc.upload_evidence(owner, company, '확정용.txt', raw)
second = svc.upload_evidence(owner, company, '후보용.txt', base64.b64encode(b'SYNTHETIC UI CANDIDATE - NOT REAL TAX DATA\\n').decode())
from price.models import TransactionInput
tx = svc.create_transaction(owner, company, TransactionInput(external_id='ui-evidence-1', direction='purchase', event_date='2026-10-31', counterparty='합성 거래처', net=1000, tax=100, gross=1100, tax_category='standard', business_purpose='화면 증빙 연결', evidence_ids=[]))
print(json.dumps({'company': company, 'transaction': tx['id'], 'money': [tx['net'], tx['tax'], tx['gross']]}))
`;
  return JSON.parse(execFileSync('python3', ['-c', code], {cwd: root, encoding: 'utf8'}));
}

const report = {status: 'FAIL', checks: [], port: 0, production_port_touched: false};
try {
  const db = path.join(temp, 'price.sqlite3');
  const seeded = seed(db);
  const apiPort = await freePort();
  if (apiPort === 4382) throw new Error('refusing production port 4382');
  report.port = apiPort;
  const chromePort = await freePort();
  const url = `http://127.0.0.1:${apiPort}`;
  const log = fs.openSync(path.join(temp, 'server.log'), 'w');
  const server = spawn('python3', ['-m', 'price', '--db', db, 'serve', '--host', '127.0.0.1', '--port', String(apiPort)], {cwd: root, stdio: ['ignore', log, log]});
  children.push(server);
  const wait = async (fn, label, ms = 15000) => {
    const end = Date.now() + ms;
    let last;
    while (Date.now() < end) {
      try { if (await fn()) return; } catch (error) { last = error; }
      await pause(100);
    }
    throw new Error('Timeout: ' + label + (last ? ' ' + last.message : ''));
  };
  await wait(async () => {
    if (server.exitCode !== null) throw new Error('API exited');
    return (await fetch(url + '/health')).ok;
  }, 'API health');
  const chromeLog = fs.openSync(path.join(temp, 'chrome.log'), 'w');
  const chrome = spawn('/usr/bin/google-chrome-stable', ['--headless=new', '--disable-background-networking', '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check', '--user-data-dir=' + path.join(temp, 'profile'), '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + chromePort, 'about:blank'], {cwd: root, stdio: ['ignore', chromeLog, chromeLog]});
  children.push(chrome);
  await wait(async () => {
    if (chrome.exitCode !== null) throw new Error('Chrome exited');
    return (await fetch(`http://127.0.0.1:${chromePort}/json/version`)).ok;
  }, 'Chrome DevTools');
  const target = await (await fetch(`http://127.0.0.1:${chromePort}/json/new?about:blank`, {method: 'PUT'})).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, {once: true}); ws.addEventListener('error', reject, {once: true}); });
  const pending = new Map();
  let seq = 0;
  ws.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!message.id) return;
    const item = pending.get(message.id);
    if (!item) return;
    pending.delete(message.id);
    clearTimeout(item.timer);
    message.error ? item.reject(new Error(JSON.stringify(message.error))) : item.resolve(message.result);
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout ' + method)); }, 15000);
    pending.set(id, {resolve, reject, timer});
    ws.send(JSON.stringify({id, method, params}));
  });
  const evaluate = async expression => {
    const result = await cdp('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ' ' + (result.exceptionDetails.exception?.description || ''));
    return result.result.value;
  };
  const q = JSON.stringify;
  const click = async selector => {
    await wait(() => evaluate(`!!document.querySelector(${q(selector)})`), 'element ' + selector);
    const rect = await evaluate(`(()=>{const e=document.querySelector(${q(selector)});e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await cdp('Input.dispatchMouseEvent', {type: 'mousePressed', ...rect, button: 'left', clickCount: 1});
    await cdp('Input.dispatchMouseEvent', {type: 'mouseReleased', ...rect, button: 'left', clickCount: 1});
  };
  const check = (name, value = true) => { if (!value) throw new Error(name); report.checks.push(name); };
  await cdp('Runtime.enable');
  await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false});
  await cdp('Page.navigate', {url: url + '/expert'});
  await wait(() => evaluate('!!document.querySelector("#login-form")'), 'login form');
  await evaluate(`(()=>{const f=document.querySelector('#login-form');f.username.value='ui-owner';f.password.value=${q(password)};})()`);
  await click('#login-form button[type="submit"]');
  await wait(() => evaluate('!document.querySelector("#workspace").hidden'), 'workspace');
  await cdp('Page.navigate', {url: url + '/expert#transactions'});
  await wait(() => evaluate(`!!document.querySelector('[data-action="tx-detail"][data-id=${q(seeded.transaction)}]')`), 'transaction row');
  await click(`[data-action="tx-detail"][data-id="${seeded.transaction}"]`);
  await wait(() => evaluate('!!document.querySelector("#evidence-link-panel")'), 'evidence detail');
  check('detail starts with no links', await evaluate('!!document.querySelector("[data-empty=candidate]") && !!document.querySelector("[data-empty=confirmed]")'));
  await evaluate(`(()=>{const form=document.querySelector('#evidence-link-panel form[data-link-action="link"]');const option=[...form.evidence_id.options].find(item=>item.textContent==='확정용.txt');form.evidence_id.value=option.value;form.reason.value='화면에서 후보 연결';})()`);
  await click('#evidence-link-panel form[data-link-action="link"] button[type="submit"]');
  await wait(() => evaluate(`document.querySelector('[data-evidence-status="candidate"]')?.textContent.includes('확정용.txt')`), 'candidate visible');
  check('empty confirm reason is blocked', await evaluate(`(()=>{const form=document.querySelector('[data-evidence-status="candidate"] form[data-link-action="confirm"]');return form.reason.value==='' && form.checkValidity()===false;})()`));
  await evaluate(`document.querySelector('[data-evidence-status="candidate"] form[data-link-action="confirm"] textarea').value='화면에서 별도 확정'`);
  await click('[data-evidence-status="candidate"] form[data-link-action="confirm"] button[type="submit"]');
  await wait(() => evaluate(`document.querySelector('[data-evidence-status="confirmed"]')?.textContent.includes('확정용.txt')`), 'confirmed visible');
  await evaluate(`(()=>{const form=document.querySelector('#evidence-link-panel form[data-link-action="link"]');const option=[...form.evidence_id.options].find(item=>item.textContent==='후보용.txt');form.evidence_id.value=option.value;form.reason.value='화면 후보';})()`);
  await click('#evidence-link-panel form[data-link-action="link"] button[type="submit"]');
  await wait(() => evaluate(`document.querySelector('[data-evidence-status="candidate"]')?.textContent.includes('후보용.txt') && document.querySelector('[data-evidence-status="confirmed"]')?.textContent.includes('확정용.txt')`), 'both statuses visible');
  await evaluate(`document.querySelector('#toast').hidden=true`);
  const image = await cdp('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  fs.writeFileSync(shot, Buffer.from(image.data, 'base64'));
  report.screenshot = shot;
  await click('[data-evidence-status="candidate"] [data-link-action="unlink"]');
  await wait(() => evaluate('!document.querySelector("[data-evidence-status=candidate]") && !!document.querySelector("[data-evidence-status=confirmed]")'), 'candidate unlinked');
  const after = await evaluate(`fetch('/api/companies/${seeded.company}/transactions/${seeded.transaction}',{credentials:'same-origin'}).then(r=>r.json())`);
  const journals = await evaluate(`fetch('/api/companies/${seeded.company}/journals',{credentials:'same-origin'}).then(r=>r.json())`);
  check('reread keeps confirmed only', after.evidence_links.length === 1 && after.evidence_links[0].status === 'confirmed' && after.evidence_links[0].confirm_reason === '화면에서 별도 확정');
  check('amounts unchanged', after.net === 1000 && after.tax === 100 && after.gross === 1100);
  check('no journal created', Array.isArray(journals) && journals.length === 0);
  report.status = 'PASS';
  ws.close();
} catch (error) {
  report.error = String(error.message || error);
  process.exitCode = 1;
} finally {
  for (const child of children.reverse()) if (child.exitCode === null) child.kill('SIGTERM');
  await pause(300);
  console.log(JSON.stringify(report));
}
