import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const owned = mkdtempSync(path.join(tmpdir(), 'price-qa-invalidation-'));
const shots = path.join(owned, 'shots');
mkdirSync(shots);
const children = [];
const report = { checks: [], screenshots: [], owned };
const q = JSON.stringify;

function fail(name) { throw new Error(name); }
function check(name, condition) { if (!condition) fail(name); report.checks.push({ name, status: 'PASS' }); }
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}
function onceLine(stream, pattern) {
  return new Promise((resolve, reject) => {
    let buffer = '';
    const timer = setTimeout(() => reject(new Error('timeout ' + pattern)), 20000);
    const onData = (chunk) => {
      buffer += chunk.toString();
      if (buffer.includes(pattern)) {
        clearTimeout(timer);
        stream.off('data', onData);
        resolve(buffer);
      }
    };
    stream.on('data', onData);
  });
}
function cdp(ws) {
  let id = 0;
  const pending = new Map();
  const listeners = new Map();
  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const call = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(call.timer);
      message.error ? call.reject(new Error(JSON.stringify(message.error))) : call.resolve(message.result);
    } else if (message.method) {
      for (const listener of listeners.get(message.method) || []) listener(message.params);
    }
  });
  return {
    send(method, params = {}) {
      const key = ++id;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(key); reject(new Error('timeout ' + method)); }, 20000);
        pending.set(key, { resolve, reject, timer });
        ws.send(JSON.stringify({ id: key, method, params }));
      });
    },
    once(method, filter) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('timeout ' + method)), 20000);
        const listener = (params) => {
          if (filter && !filter(params)) return;
          clearTimeout(timer);
          listeners.get(method).delete(listener);
          resolve(params);
        };
        if (!listeners.has(method)) listeners.set(method, new Set());
        listeners.get(method).add(listener);
      });
    },
  };
}
async function evaluate(client, expression) {
  const result = await client.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  return result.result.value;
}
async function login(client, username, password) {
  await evaluate(client, `new Promise((resolve, reject) => {
    const workspace = document.querySelector('#owner-workspace');
    const form = document.querySelector('#owner-login-form');
    const observer = new MutationObserver(() => {
      if (!workspace.hidden) { observer.disconnect(); resolve('ready'); }
    });
    observer.observe(workspace, { attributes: true, attributeFilter: ['hidden'] });
    form.elements.username.value = ${q(username)};
    form.elements.password.value = ${q(password)};
    form.requestSubmit();
    form.addEventListener('submit', () => {}, { once: true });
  })`);
}
async function logout(client) {
  await evaluate(client, `new Promise((resolve) => {
    const login = document.querySelector('#owner-login');
    const observer = new MutationObserver(() => {
      if (!login.hidden) { observer.disconnect(); resolve('logged-out'); }
    });
    observer.observe(login, { attributes: true, attributeFilter: ['hidden'] });
    document.querySelector('#owner-logout').click();
  })`);
}
async function retainedCards(client, ids) {
  return evaluate(client, `(() => {
    const present = {};
    for (const id of ${JSON.stringify(ids)}) present[id] = Boolean(document.querySelector('[data-discrepancy="' + id + '"]'));
    return present;
  })()`);
}
async function openQuestions(client) {
  const response = client.once('Network.responseReceived', (params) => params.response.url.includes('/discrepancies') && !params.response.url.includes('/invalidate'));
  await evaluate(client, `document.querySelector('[data-view="questions"]').click()`);
  const received = await response;
  check('discrepancies loaded', received.response.status === 200);
  await evaluate(client, `new Promise((resolve, reject) => {
    const ready = () => {
      const root = document.querySelector('#discrepancy-inbox');
      return root && (root.querySelector('[data-discrepancy]') || root.querySelector('.empty') || root.querySelector('.error'));
    };
    if (ready()) return resolve('ready');
    const root = document.body;
    const observer = new MutationObserver(() => { if (ready()) { observer.disconnect(); resolve('ready'); } });
    observer.observe(root, { childList: true, subtree: true });
    setTimeout(() => { observer.disconnect(); reject(new Error('inbox did not render')); }, 15000);
  })`);
}
async function clickInvalidate(client, id, expected) {
  return evaluate(client, `new Promise((resolve, reject) => {
    const finish = (card, denial) => resolve({ state: card.dataset.state, denial: denial ? denial.textContent : '', text: card.textContent });
    const arm = (button) => {
      const root = document.querySelector('#discrepancy-inbox');
      let clicked = false;
      const observer = new MutationObserver(() => {
        if (!clicked) return;
        const card = document.querySelector('[data-discrepancy=${q(id)}]');
        if (!card) return;
        const denial = card.querySelector('[data-denial="invalidate"]');
        if (card.dataset.state === ${q(expected.state)} || denial) {
          observer.disconnect();
          finish(card, denial);
        }
      });
      observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-state'] });
      clicked = true;
      button.click();
    };
    const button = document.querySelector('[data-invalidate=${q(id)}]');
    if (button) return arm(button);
    const root = document.body;
    const waiter = new MutationObserver(() => {
      const found = document.querySelector('[data-invalidate=${q(id)}]');
      if (!found) return;
      waiter.disconnect();
      arm(found);
    });
    waiter.observe(root, { childList: true, subtree: true });
    setTimeout(() => { waiter.disconnect(); reject(new Error('missing invalidate button')); }, 15000);
  })`);
}
async function cardState(client, id) {
  return evaluate(client, `(() => {
    const card = document.querySelector('[data-discrepancy=${q(id)}]');
    return card ? { state: card.dataset.state, text: card.innerText, present: true } : { present: false };
  })()`);
}
async function shoot(client, name, id) {
  if (id) {
    await evaluate(client, `(() => {
      const card = document.querySelector('[data-discrepancy=${q(id)}]');
      if (!card) return;
      card.open = true;
      card.scrollIntoView({ block: 'start' });
    })()`);
  }
  const shot = await client.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const file = path.join(shots, name);
  writeFileSync(file, Buffer.from(shot.data, 'base64'));
  report.screenshots.push(file);
  return file;
}

let chrome;
try {
  const db = path.join(owned, 'qa.sqlite3');
  const seedPath = path.join(owned, 'seed.json');
  const seed = spawn('python3', ['scripts/seed_discrepancy_qa.py', db, seedPath], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  children.push(seed);
  let seedErr = '';
  seed.stderr.on('data', (chunk) => { seedErr += chunk; });
  const seedExit = await new Promise((resolve) => seed.once('exit', resolve));
  if (seedExit !== 0) fail('seed exit ' + seedExit + ' ' + seedErr.slice(-500));
  const fixture = JSON.parse(readFileSync(seedPath, 'utf8'));
  const apiPort = await freePort();
  const chromePort = await freePort();
  const origin = 'http://127.0.0.1:' + apiPort;
  const api = spawn('python3', ['-m', 'price', '--db', db, 'serve', '--host', '127.0.0.1', '--port', String(apiPort)], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  children.push(api);
  await onceLine(api.stderr, 'Uvicorn running');
  chrome = spawn('/usr/bin/google-chrome', [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--user-data-dir=' + path.join(owned, 'chrome'),
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + chromePort, 'about:blank',
  ], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  children.push(chrome);
  await onceLine(chrome.stderr, 'DevTools listening');
  const created = await (await fetch('http://127.0.0.1:' + chromePort + '/json/new?about:blank', { method: 'PUT' })).json();
  const ws = new WebSocket(created.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  const client = cdp(ws);
  await client.send('Network.enable');
  await client.send('Page.enable');
  await client.send('Runtime.enable');
  const loaded = client.once('Page.loadEventFired');
  await client.send('Page.navigate', { url: origin + '/' });
  await loaded;
  for (const viewport of [
    { name: 'desktop', width: 1440, height: 900, mobile: false },
    { name: 'mobile', width: 390, height: 844, mobile: true },
  ]) {
    for (const scheme of ['light', 'dark']) {
      await client.send('Emulation.setDeviceMetricsOverride', { width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: viewport.mobile });
      await client.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: scheme }] });
      const label = viewport.name + '-' + scheme;
      const reset = spawn('python3', ['scripts/seed_discrepancy_qa.py', 'reset', db, fixture.company, fixture.changed_review], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
      const resetExit = await new Promise((resolve) => reset.once('exit', resolve));
      if (resetExit !== 0) fail(label + ' reset');
      await login(client, fixture.owner, fixture.password);
      const ownerRetained = await retainedCards(client, [fixture.changed_review, fixture.equal_review, fixture.foreign_review]);
      check(label + ' owner session starts without retained cards', Object.values(ownerRetained).every((present) => present === false));
      await openQuestions(client);
      const changed = await cardState(client, fixture.changed_review);
      const equal = await cardState(client, fixture.equal_review);
      const foreign = await cardState(client, fixture.foreign_review);
      check(label + ' changed review visible', changed.present && changed.state === 'UNASSIGNED');
      check(label + ' equal review visible', equal.present && equal.state === 'UNASSIGNED');
      check(label + ' foreign review absent', foreign.present === false);
      const invalidated = await clickInvalidate(client, fixture.changed_review, { state: 'SOURCE_INVALIDATED' });
      check(label + ' changed marker invalidates', invalidated.state === 'SOURCE_INVALIDATED' && invalidated.text.includes('무효'));
      const preserved = await clickInvalidate(client, fixture.equal_review, { state: 'UNASSIGNED' });
      check(label + ' equal marker preserved', preserved.state === 'UNASSIGNED' && !preserved.text.includes('무효가 되었습니다'));
      const overflow = await evaluate(client, 'document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1');
      check(label + ' no horizontal overflow', overflow === true);
      await shoot(client, label + '-owner-changed.png', fixture.changed_review);
      await shoot(client, label + '-owner-equal.png', fixture.equal_review);
      await logout(client);
      await login(client, fixture.viewer, fixture.password);
      const viewerRetained = await retainedCards(client, [fixture.changed_review, fixture.equal_review, fixture.foreign_review]);
      check(label + ' viewer session drops prior cards before refresh', Object.values(viewerRetained).every((present) => present === false));
      await openQuestions(client);
      const denied = await clickInvalidate(client, fixture.equal_review, { state: 'UNASSIGNED' });
      check(label + ' viewer denial visible', denied.denial.length > 0 && denied.state === 'UNASSIGNED');
      await shoot(client, label + '-viewer.png', fixture.equal_review);
      await logout(client);
      await login(client, fixture.foreign, fixture.password);
      const foreignRetained = await retainedCards(client, [fixture.changed_review, fixture.equal_review]);
      check(label + ' foreign session drops other company before refresh', Object.values(foreignRetained).every((present) => present === false));
      await openQuestions(client);
      check(label + ' foreign ui hides other company', (await cardState(client, fixture.changed_review)).present === false && (await cardState(client, fixture.equal_review)).present === false && (await cardState(client, fixture.foreign_review)).present === true);
      await shoot(client, label + '-foreign.png', fixture.foreign_review);
      await logout(client);
    }
  }
  const png = readFileSync(report.screenshots[0]);
  check('png signature', png[0] === 0x89 && png[1] === 0x50);
  writeFileSync(path.join(owned, 'report.json'), JSON.stringify({ ...report, password: undefined, status: 'PASS' }, null, 2));
  console.log(JSON.stringify({ status: 'PASS', owned, checks: report.checks.length, screenshots: report.screenshots }, null, 2));
} catch (error) {
  writeFileSync(path.join(owned, 'report.json'), JSON.stringify({ status: 'FAIL', error: String(error), checks: report.checks, screenshots: report.screenshots }, null, 2));
  console.error(String(error));
  console.error(JSON.stringify({ status: 'FAIL', owned, checks: report.checks, screenshots: report.screenshots }));
  process.exitCode = 1;
} finally {
  for (const child of children) {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
  }
  if (process.exitCode) process.exit(process.exitCode);
}
