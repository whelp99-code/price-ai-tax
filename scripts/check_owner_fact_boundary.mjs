// Executes the owner screen against synthetic home payloads. Not a source grep.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ids = new Map();
function queryAll(rootNode, selector) {
  const found = [];
  const visit = node => {
    if (!node || node.nodeType === 'text') return;
    if (selector.startsWith('#') && node.id === selector.slice(1)) found.push(node);
    else if (selector.startsWith('.') && String(node.className || '').split(/\s+/).includes(selector.slice(1))) found.push(node);
    else if (selector === 'button' && node.tag === 'button') found.push(node);
    else if (selector === 'input' && node.tag === 'input') found.push(node);
    else if (selector === 'form' && node.tag === 'form') found.push(node);
    for (const child of node.children || []) visit(child);
  };
  visit(rootNode);
  return found;
}
function makeNode(tag = 'div') {
  const node = {
    tag, nodeType: 'element', children: [], className: '', dataset: {}, style: {}, hidden: false, value: '',
    attributes: {},
    addEventListener() {},
    setAttribute(name, value) { this.attributes[name] = value; },
    append(...items) { this.children.push(...items.filter(Boolean)); },
    replaceChildren(...items) { this.children = items.filter(Boolean); },
    querySelector(selector) { return queryAll(this, selector)[0] || null; },
    querySelectorAll(selector) { return queryAll(this, selector); },
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
  let text = '';
  let id = '';
  Object.defineProperty(node, 'textContent', {
    get() { return text || this.children.map(child => child.textContent || '').join(''); },
    set(value) { text = String(value); this.children = []; },
  });
  Object.defineProperty(node, 'id', {
    get() { return id; },
    set(value) { if (id) ids.delete(id); id = value || ''; if (id) ids.set(id, node); },
  });
  return node;
}
const body = makeNode('body');
const document = {
  body,
  createElement: tag => makeNode(tag),
  createTextNode: text => ({nodeType: 'text', textContent: String(text), children: []}),
  querySelector(selector) { return selector.startsWith('#') ? ids.get(selector.slice(1)) || null : queryAll(body, selector)[0] || null; },
  querySelectorAll(selector) { return queryAll(body, selector); },
  getElementById(id) { return ids.get(id) || null; },
};
for (const id of ['owner-login-form', 'owner-logout', 'owner-company', 'choose-files', 'owner-files', 'drop-zone', 'pause-transfer', 'cancel-transfer', 'refresh-documents', 'close-owner-dialog', 'owner-error', 'owner-toast', 'owner-workspace', 'account-controls', 'owner-login', 'owner-dialog', 'owner-dialog-title', 'owner-dialog-content', 'owner-dialog-error', 'mode-banner', 'owner-title', 'owner-stage', 'question-count', 'owner-questions', 'result-stats', 'result-notice', 'pending-summary', 'document-list', 'view-questions', 'view-results', 'view-upload']) {
  const node = makeNode('div');
  node.id = id;
  body.append(node);
}
document.getElementById('owner-files').tag = 'input';
document.getElementById('owner-logout').tag = 'button';
const routes = new Map();
let releaseStartup;
const startup = new Promise(resolve => { releaseStartup = resolve; });
const context = {
  console, document, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  Headers: class { constructor() {} set() {} },
  fetch: async requestPath => {
    const key = String(requestPath).split('?')[0];
    if (key === '/api/session' || key === '/api/login') await startup;
    return {ok: true, status: 200, json: async () => routes.get(key) ?? {}};
  },
  crypto: {randomUUID: () => '11111111111111111111111111111111'},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root, 'price/static/owner.js'), 'utf8'), context);
const home = role => ({
  company: {id: 'co-synthetic', name: '합성 회사', demo: false, role},
  owner_actions: [{kind: 'confirm_company', label: '회사와 계좌 정보를 확인해 주세요.'}],
  needs_company_confirmation: true,
  pending_instrument_candidates: [],
  documents: [],
  counts: {documents: 0, read: 0, needs_attention: 0, source_cases: 0, posted_journals: 0, context_attention: 0},
  notice: '자료 읽기는 자동으로 진행합니다.',
  accounting_complete: false,
});
function controls() {
  const questions = document.getElementById('owner-questions');
  const dialog = document.getElementById('owner-dialog-content');
  return {
    account_entry: questions.querySelectorAll('button').length,
    fact_inputs: dialog.querySelectorAll('input').length,
    save_buttons: dialog.querySelectorAll('button').filter(node => node.type === 'submit').length,
  };
}
const result = {};
for (const role of ['viewer', 'owner']) {
  document.getElementById('owner-dialog-content').replaceChildren();
  document.getElementById('owner-dialog').open = false;
  const payload = home(role);
  routes.set('/api/companies/co-synthetic/owner/home', payload);
  routes.set('/api/companies/co-synthetic/uploads', []);
  routes.set('/api/companies/co-synthetic/owner/questions', {questions: [], source_changed: 0});
  routes.set('/api/companies/co-synthetic/owner/events', {events: []});
  routes.set('/api/companies/co-synthetic/owner/onboarding', {notice: '회사와 계좌의 사실 확인입니다.', facts: null, revision: 0, instrument_candidates: [], company_candidates: [], profile_name: '합성 회사', mode: 'company'});
  await context.acceptSession({csrf: 'synthetic', companies: [{id: 'co-synthetic', name: '합성 회사'}]});
  context.renderHome(payload, []);
  await context.showOnboarding();
  result[role] = controls();
}
process.stdout.write(JSON.stringify(result));
releaseStartup();
