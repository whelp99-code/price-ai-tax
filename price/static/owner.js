'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {csrf: '', companies: [], company: '', upload: null, controller: null, uploading: false, refreshing: false, home: null};
const statusNames = {QUEUED:'읽기 대기', PROCESSING:'읽는 중', NORMALIZED:'자료 읽기 완료', VALIDATION_REQUIRED:'원본 확인 필요', FORMAT_REVIEW:'양식 확인 필요', READER_REQUIRED:'읽기 지원 준비 중', QUARANTINED:'안전 확인 필요', READ_FAILED:'읽기 실패', PERMISSION_REVIEW:'접근 권한 확인 필요', COMPANY_REVIEW:'회사 정보 확인 필요'};
const kindNames = {bank_statement:'통장내역', card_transactions:'카드내역', tax_invoice_list:'세금계산서 목록', company_registration:'사업자등록증', html_tables:'원본 표 내역', mixed_workbook:'여러 종류의 표', unknown:'자료 종류 확인 필요'};
const issueNames = {TABLE_HEADER_UNKNOWN:'이 자료의 표 형식을 아직 확인하지 못했습니다.', TABLE_READER_REQUIRED:'엑셀 파일의 자동 읽기는 준비 중입니다.', LEGACY_TABLE_READER_REQUIRED:'이전 형식의 엑셀 자료를 읽을 준비가 필요합니다.', DOCUMENT_READER_REQUIRED:'PDF 원본을 보관했습니다. 내용 읽기는 준비 중입니다.', IMAGE_READER_REQUIRED:'사진 원본을 보관했습니다. 내용 인식은 준비 중입니다.', PASSWORD_REQUIRED:'암호가 걸린 자료입니다. 암호 입력 기능은 아직 준비 중입니다.', INVOICE_TOTAL_MISMATCH:'공급가액·세액과 원본 합계가 맞지 않습니다.', CONTROL_TOTAL_MISMATCH:'읽은 금액과 원본에 적힌 합계가 맞지 않습니다.', COLUMN_COUNT_MISMATCH:'일부 행의 열 구성이 다릅니다.', AMOUNT_INVALID:'금액을 확실하게 읽지 못한 부분이 있습니다.', TABLE_RESOURCE_LIMIT:'안전하게 읽을 수 있는 자료 처리 한도를 넘었습니다.', ACTIVE_CONTENT_REVIEW:'실행 가능한 내용이 의심되어 자동 처리를 보류했습니다.', COMPANY_SOURCE_CONFLICT:'확인된 회사 정보와 자료의 계좌 또는 사업자가 맞지 않습니다.', RUNNING_BALANCE_MISMATCH:'거래 금액과 다음 잔액이 맞지 않습니다. 원본을 확인해야 합니다.', BANK_ORDER_REVIEW:'거래 순서와 잔액을 함께 확인해야 합니다.', CONTROL_CURRENCY_AMBIGUOUS:'서로 다른 통화가 섞여 원본 합계를 검증할 수 없습니다.', XML_REQUIRED_FIELD:'XML 원본에 필요한 항목이 없습니다.', XML_AMBIGUOUS_FIELD:'XML에 같은 항목이 여러 번 있어 확인이 필요합니다.', LINE_TOTAL_MISMATCH:'품목 합계와 문서 합계가 맞지 않습니다.'};
function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); if (className) node.className = className; return node; }
function button(text, action, className='quiet') { const node = element('button', text, className); node.type = 'button'; node.addEventListener('click', () => Promise.resolve(action()).catch(showError)); return node; }
function prefix(path) { if (!state.company) throw new Error('먼저 회사를 선택해 주세요.'); return '/api/companies/' + encodeURIComponent(state.company) + path; }
function showError(error) { $('#owner-error').textContent = error.message || String(error); }
function toast(text) { const node = $('#owner-toast'); node.textContent = text; node.hidden = false; setTimeout(() => { node.hidden = true; }, 5000); }
async function request(path, options={}) {
  const headers = new Headers(options.headers || {});
  const method = options.method || 'GET';
  if (!['GET','HEAD'].includes(method)) headers.set('x-csrf-token', state.csrf);
  let response;
  try { response = await fetch(path, {...options, headers, credentials:'same-origin'}); }
  catch (error) { if (error.name === 'AbortError') throw error; throw new Error('서버 연결을 확인해 주세요. 이미 받은 자료 조각은 유지됩니다.'); }
  let result; try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) { if (response.status === 401 && path !== '/api/login') showLogin(); const error = new Error(result.message || ('요청을 처리하지 못했습니다. (' + response.status + ')')); error.code = result.code; throw error; }
  return result;
}
function post(path, data) { return request(prefix(path), {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(data)}); }
function showLogin() { state.controller?.abort(); state.csrf = ''; state.company = ''; $('#owner-workspace').hidden = true; $('#account-controls').hidden = true; $('#owner-login').hidden = false; }
async function acceptSession(result) {
  state.csrf = result.csrf; state.companies = result.companies || [];
  const select = $('#owner-company'); select.replaceChildren();
  for (const company of state.companies) { const item = element('option', company.name); item.value = company.id; select.append(item); }
  if (!state.companies.length) throw new Error('이 계정에 연결된 회사가 없습니다. 운영자 확인이 필요합니다.');
  state.company = state.companies[0].id;
  $('#owner-login').hidden = true; $('#owner-workspace').hidden = false; $('#account-controls').hidden = false;
  await refresh();
}
function chooseView(view) {
  for (const node of document.querySelectorAll('.owner-view')) node.hidden = node.id !== 'view-' + view;
  for (const node of document.querySelectorAll('[data-view]')) node.setAttribute('aria-selected', String(node.dataset.view === view));
}
function descriptionFor(doc) {
  if (doc.state === 'NO_ACTIVITY_REPORTED') return '원본에 거래 없음과 조회기간이 표시되어 있습니다. 전체 자료 확인·마감 완료로 확정된 것은 아닙니다.';
  if (doc.state === 'NORMALIZED') return (kindNames[doc.document_kind] || '원본 자료') + ' · ' + doc.record_count + '개 기록을 읽었습니다. 회계 처리는 아직 완료되지 않았습니다.';
  const issue = (doc.issues || [])[0];
  const code = issue?.code || doc.inspection?.code;
  return issueNames[code] || '원본은 보존했습니다. 자료 읽기 또는 내부 확인이 필요합니다.';
}
function renderDocuments(home, batches) {
  const list = $('#document-list'); list.replaceChildren();
  for (const batch of batches.filter(x => x.state === 'OPEN')) {
    const node = element('div', undefined, 'document');
    const text = element('div'); text.append(element('strong', '이어서 보낼 자료 ' + batch.files.length + '개'), element('p', '같은 파일들을 다시 선택하면 받은 부분부터 이어서 전송합니다.'));
    const actions = element('div', undefined, 'actions'); actions.append(element('span','전송 대기','pill attention'), button('전송 취소', async () => { await post('/uploads/' + batch.id + '/cancel', {expected_revision:batch.revision, reason:'대표 자료함에서 미완료 전송 취소'}); await refresh(); }));
    node.append(text, actions); list.append(node);
  }
  for (const doc of home.documents) {
    const node = element('div', undefined, 'document'); node.dataset.document = doc.id;
    const text = element('div'); text.append(element('strong', doc.filename), element('p', descriptionFor(doc)));
    const actions = element('div', undefined, 'actions');
    const attention = !['NORMALIZED','QUEUED','PROCESSING'].includes(doc.state);
    actions.append(element('span', statusNames[doc.state] || '확인 필요', 'pill' + (attention ? ' attention' : '')));
    if (doc.record_count) actions.append(button('읽은 내용', () => showExtraction(doc.id, doc.filename)));
    if (['READ_FAILED','FORMAT_REVIEW','VALIDATION_REQUIRED','COMPANY_REVIEW','PERMISSION_REVIEW'].includes(doc.state) && home.company.role !== 'viewer') {
      actions.append(button('다시 읽기', async () => {
        await post('/documents/' + encodeURIComponent(doc.id) + '/reprocess', {expected_revision:doc.revision,idempotency_key:crypto.randomUUID(),reason:'대표 자료함에서 원본 재확인 요청'});
        toast('원본을 보존하고 다시 읽기를 시작했습니다.'); await refresh();
      }));
    }

    node.append(text, actions); list.append(node);
  }
  if (!list.children.length) list.append(element('div','아직 올린 자료가 없습니다. 가지고 있는 파일부터 선택해 주세요.','empty'));
}
function renderHome(home, batches) {
  state.home = home;
  $('#mode-banner').textContent = (home.company.demo ? '합성 데모 자료함 · 실제 회사 자료와 구분합니다. ' : '실제 회사 공간 · 개발 검증 상태입니다. ') + '현재는 자료 읽기 단계이며 실제 신고·납부는 실행되지 않습니다.';
  $('#owner-title').textContent = home.company.name + '의 자료를 맡겨주세요.';
  $('#owner-stage').textContent = '자료는 한곳에, 필요한 확인은 쉬운 말로. 처리되지 않은 항목도 그대로 보여드립니다.';
  $('#question-count').textContent = home.owner_actions.length ? '(' + home.owner_actions.length + ')' : '';
  renderDocuments(home, batches);
  const questions = $('#owner-questions'); questions.replaceChildren();
  const pendingInstruments = home.pending_instrument_candidates || [];
  if (home.needs_company_confirmation || pendingInstruments.length) {
    const card = element('div', undefined, 'note');
    card.append(element('p', home.needs_company_confirmation ? '이 자료함의 회사와 회사 소유 계좌 정보를 확인해 주세요.' : '원본에서 계좌·카드 후보를 찾았습니다. 우리 회사 자료인 것만 선택해 주세요.'), button('회사 계좌·카드 확인', showOnboarding, 'primary'));
    questions.append(card);
  } else questions.append(element('p','현재 대표님께 추가로 요청한 회사 사실이 없습니다.'));
  if (home.counts.needs_attention) questions.append(element('p', '자료 ' + home.counts.needs_attention + '개가 추가 확인 또는 읽기 지원을 기다립니다. 대표님께 회계 판단을 요청한 것은 아닙니다.', 'note'));
  questions.append(element('p','거래 목적은 쉬운 업무 사실로 확인합니다. 자동기장 정책·전문 검토가 아직 연결되지 않은 업무는 완료로 표시하지 않습니다.','small'));
  const stats = $('#result-stats'); stats.replaceChildren();
  for (const [label, value] of [['올린 자료',home.counts.documents],['읽기 완료',home.counts.read],['추가 확인',home.counts.needs_attention]]) { const box = element('div',undefined,'stat'); box.append(element('span',label), element('strong',value + '개')); stats.append(box); }
  $('#result-notice').textContent = home.notice;
  const pending = $('#pending-summary'); pending.replaceChildren();
  pending.append(element('p', '읽은 자료에서 ' + home.counts.source_cases + '개의 거래 확인 작업이 만들어졌습니다. 아직 다른 자료 연결과 회계 검증을 거치기 전입니다.','note'));
  pending.append(element('p', '기존 검증 장부의 확정 기록: ' + home.counts.posted_journals + '건. 이번 업로드의 자동기장 완료 건수와는 다릅니다.'));
  pending.append(element('p','실제 기관별 원본 양식의 검증과 최종 세목·신고 기능은 아직 인수 전입니다.','small'));
  if(typeof renderBusinessFacts==='function')renderBusinessFacts();
}
async function refresh() {
  if (!state.company || state.refreshing || state.uploading) return;
  state.refreshing = true;
  const company = state.company;
  try { const [home, batches] = await Promise.all([request(prefix('/owner/home')), request(prefix('/uploads'))]); if (company === state.company) renderHome(home,batches); }
  finally { state.refreshing = false; }
}
function openDialog(title) { $('#owner-dialog-title').textContent = title; $('#owner-dialog-content').replaceChildren(); $('#owner-dialog-error').textContent = ''; $('#owner-dialog').showModal(); return $('#owner-dialog-content'); }
async function showExtraction(id, filename) {
  const root = openDialog(filename + ' · 읽은 내용'); root.append(element('p','원본에서 읽은 값입니다. 회계·세무 판단 또는 원장 확정 결과가 아닙니다.'));
  try {
    const result = await request(prefix('/documents/' + encodeURIComponent(id) + '/extraction?limit=100'));
    const wrap = element('div',undefined,'table-wrap'), table = element('table');
    const head = element('tr'); for (const label of ['원본 행','내용','금액','확인 상태']) head.append(element('th',label)); table.append(head);
    for (const record of result.rows) {
      const value = record.values, row = element('tr');
      for (const text of [record.row, value.description || value.reference || record.kind, (value.gross ?? '미확인') + ' ' + (value.currency || '(통화 확인 필요)'), record.validation === 'NORMALIZED' ? '읽기 확인 · 회계 처리 전' : '원본 확인 필요']) row.append(element('td',text));
      table.append(row);
    }
    wrap.append(table); root.append(wrap);
    const history = await request(prefix('/documents/' + encodeURIComponent(id) + '/extraction/history'));
    if (history.runs.length > 1) {
      const details = element('details'), summary = element('summary', '다시 읽기 이력 ' + history.total + '회'); details.append(summary);
      for (const run of history.runs) details.append(element('p', run.generation + '차 읽기 · ' + (statusNames[run.summary.state] || run.summary.state) + ' · 바뀐 항목 ' + run.value_change_count + '개'));
      root.append(details);
    }

    if (result.next_offset !== null) root.append(element('p','앞의 100개 기록을 표시했습니다. 전체 ' + result.total + '개 기록은 원본 위치와 함께 보관되어 있습니다.','small'));
  } catch (error) { $('#owner-dialog-error').textContent = error.message; }
}
async function showOnboarding() {
  const current = await request(prefix('/owner/onboarding'));
  const root = openDialog('회사 정보 확인'); root.append(element('p', current.notice));
  const form = document.createElement('form'); form.id = 'company-facts-form';
  function field(label, name, value, required=false) { const box = element('label',label); const input = document.createElement('input'); input.name=name; input.value=value||''; input.required=required; input.maxLength=name==='name'?120:12; box.append(input); form.append(box); return input; }
  const candidates=current.company_candidates||[];
  const unique=new Set(candidates.map(x=>x.name+'|'+x.business_number));
  const candidate=unique.size===1?candidates[0]:null;
  if(candidate&&!current.facts)root.append(element('p','사업자등록증에서 읽은 회사 정보입니다. 사실과 맞는지 확인해 주세요.','note'));
  if(unique.size>1)root.append(element('p','서로 다른 회사 정보가 있는 자료를 발견했습니다. 이 자료함의 회사 정보를 확인해 주세요.','note'));
  const name = field('회사 이름','name',current.facts?.name || candidate?.name || current.profile_name,true);
  const number = field('사업자등록번호 (자료에 없으면 비워두세요)','business_number',current.facts?.business_number || candidate?.business_number || '');
  const checks = [];
  const seen = new Set();
  for (const instrument of [...(current.facts?.instruments || []), ...current.instrument_candidates]) {
    const key = instrument.kind + ':' + instrument.identifier; if (seen.has(key)) continue; seen.add(key);
    const label = element('label',undefined,'check'); const box = document.createElement('input'); box.type='checkbox';
    box.checked = Boolean(current.facts?.instruments?.some(x=>x.kind===instrument.kind && x.identifier===instrument.identifier));
    label.append(box, document.createTextNode((instrument.kind==='bank'?'계좌 ':'카드 ') + instrument.identifier + '는 우리 회사 자료입니다.'));
    form.append(label); checks.push({box,instrument});
  }
  const confirmation = element('label',undefined,'check'), agree = document.createElement('input'); agree.type='checkbox'; agree.required=true; agree.name='confirmed';
  confirmation.append(agree, document.createTextNode('위 내용이 회사의 실제 정보임을 확인했습니다.')); form.append(confirmation);
  const submit = element('button','확인한 사실 저장','primary'); submit.type='submit'; form.append(submit);
  form.addEventListener('submit', async event => { event.preventDefault(); submit.disabled=true; try { const saved = await post('/owner/onboarding',{expected_revision:current.revision,name:name.value,business_number:number.value,mode:current.mode,source_document_id:candidate?.source_document_id||null,instruments:checks.filter(x=>x.box.checked).map(x=>({kind:x.instrument.kind,identifier:x.instrument.identifier,currency:x.instrument.currency||null})),confirmed:agree.checked}); $('#owner-dialog').close(); state.caseworkResumeFor=''; await refresh(); if (saved.casework_resume && saved.casework_resume.state !== 'ready') showError(new Error(saved.casework_resume.message || '바뀐 자료의 거래 연결을 다시 확인하지 못했습니다.')); else toast('회사 사실을 저장했습니다.'); } catch(error){$('#owner-dialog-error').textContent=error.message;} finally{submit.disabled=false;} });
  root.append(form);
}
async function sha256(buffer) { if (!crypto.subtle) throw new Error('안전한 파일 해시 기능을 사용할 수 있는 브라우저가 필요합니다.'); const hash = await crypto.subtle.digest('SHA-256', buffer); return Array.from(new Uint8Array(hash), n=>n.toString(16).padStart(2,'0')).join(''); }
function setUploading(value) { state.uploading=value; $('#choose-files').disabled=value; $('#owner-company').disabled=value; $('#pause-transfer').disabled=!value; }
async function uploadFiles(selected) {
  if (state.uploading || !selected.length) return;
  $('#owner-error').textContent='';
  const files = [...selected];
  if (files.length>100 || files.some(f=>!f.size || f.size>50000000) || files.reduce((a,f)=>a+f.size,0)>500000000) throw new Error('파일당 50 MB, 한 번에 100개·총 500 MB 이내의 자료를 선택해 주세요.');
  setUploading(true); $('#transfer-panel').hidden=false; $('#transfer-label').textContent='원본 확인 중'; $('#transfer-message').textContent='파일 해시를 확인합니다. 파일 내용은 브라우저에 별도로 저장하지 않습니다.';
  state.controller=new AbortController();
  try {
    const prepared=[];
    for (const file of files) { if(state.controller.signal.aborted) throw new DOMException('Paused','AbortError'); prepared.push({file,hash:await sha256(await file.arrayBuffer())}); }
    prepared.sort((a,b)=>a.file.name<b.file.name?-1:a.file.name>b.file.name?1:a.hash.localeCompare(b.hash));
    const manifest=prepared.map((item,i)=>({client_file_id:'f'+i+'_'+item.hash.slice(0,24),filename:item.file.name,size_bytes:item.file.size,sha256:item.hash}));
    const baseKey='upload_'+await sha256(new TextEncoder().encode(JSON.stringify(manifest)));
    const storageKey='price.transport.'+state.company+'.'+baseKey;
    let key=sessionStorage.getItem(storageKey)||baseKey;
    if(state.controller.signal.aborted) throw new DOMException('Paused','AbortError');
    let batch=await post('/uploads',{idempotency_key:key,files:manifest});
    if(batch.state==='CANCELLED') { key=baseKey+'_'+crypto.randomUUID(); sessionStorage.setItem(storageKey,key); batch=await post('/uploads',{idempotency_key:key,files:manifest}); }
    state.upload=batch;
    const total=prepared.reduce((a,p)=>a+p.file.size,0); let sent=0;
    $('#transfer-progress').max=total;
    if(batch.state!=='SEALED') {
      for(let i=0;i<prepared.length;i++) {
        const item=manifest[i], file=prepared[i].file;
        const received=new Map(batch.files.find(x=>x.client_file_id===item.client_file_id).received_chunks.map(x=>[x.chunk_index,x]));
        $('#transfer-label').textContent=(i+1)+' / '+prepared.length+' · '+file.name;
        for(let offset=0,n=0;offset<file.size;offset+=1000000,n++) {
          if(state.controller.signal.aborted) throw new DOMException('Paused','AbortError');
          const part=await file.slice(offset,offset+1000000).arrayBuffer(); const hash=await sha256(part);
          if(received.get(n)?.sha256!==hash) await request(prefix('/uploads/'+batch.id+'/files/'+item.client_file_id+'/chunks/'+n),{method:'PUT',headers:{'content-type':'application/octet-stream','x-chunk-sha256':hash},body:part,signal:state.controller.signal});
          sent+=part.byteLength; $('#transfer-progress').value=sent; $('#transfer-message').textContent=Math.round(sent/total*100)+'% 전송 · 완료된 조각은 다시 보내지 않습니다.';
        }
      }
      batch=await post('/uploads/'+batch.id+'/complete',{expected_revision:batch.revision}); state.upload=batch;
    }
    const questions=batch.owner_questions||[];
    const pending=batch.reading_pending===true;
    const attention=(batch.reading_attention||[]).length;
    const done=pending?'이번 자료 읽기가 끝나지 않았습니다. 앞에 쌓인 다른 자료는 대신 처리하지 않았습니다.':attention?'확인이 필요한 자료가 '+attention+'개 있습니다. 읽기 완료로 표시하지 않습니다.':questions.length?'확인할 사실이 '+questions.length+'건 있습니다. '+(questions[0].text||''):'읽기·연결 결과를 반영했습니다.';
    if(questions.length)chooseView('questions');
    $('#transfer-progress').value=total; $('#transfer-label').textContent=pending?'자료 읽기 대기':'자료 접수 완료'; $('#transfer-message').textContent='원본을 보관하고 '+done+' 장부 확정·세금 신고가 완료됐다는 의미는 아닙니다.'; toast(pending?'원본은 보관됐고 이번 자료 읽기는 아직입니다.':'원본 자료를 접수했습니다.');
  } catch(error) { $('#transfer-message').textContent=error.name==='AbortError'?'전송을 잠시 멈췄습니다. 같은 파일을 다시 선택하면 이어서 진행합니다.':error.message; if(error.name!=='AbortError') showError(error); }
  finally { setUploading(false); $('#owner-files').value=''; await refresh().catch(showError); }
}
$('#owner-login-form').addEventListener('submit', async event=>{event.preventDefault(); const form=event.currentTarget; const submit=form.querySelector('button');submit.disabled=true;$('#login-error').textContent='';try{const result=await request('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:form.elements.username.value,password:form.elements.password.value})});form.elements.password.value='';await acceptSession(result);}catch(error){$('#login-error').textContent=error.message;}finally{submit.disabled=false;}});
$('#owner-logout').addEventListener('click',async()=>{state.controller?.abort();try{await request('/api/logout',{method:'POST',headers:{'content-type':'application/json'},body:'{}'});}finally{showLogin();}});
$('#owner-company').addEventListener('change',async event=>{state.company=event.target.value;state.upload=null;await refresh().catch(showError);});
for(const node of document.querySelectorAll('[data-view]')) node.addEventListener('click',()=>chooseView(node.dataset.view));
$('#choose-files').addEventListener('click',event=>{event.stopPropagation();$('#owner-files').click();});
$('#owner-files').addEventListener('change',event=>uploadFiles(event.target.files).catch(showError));
const drop=$('#drop-zone');
drop.addEventListener('click',event=>{if(event.target===drop || !['BUTTON','INPUT'].includes(event.target.tagName)) $('#owner-files').click();});
drop.addEventListener('keydown',event=>{if(event.target===drop && ['Enter',' '].includes(event.key)){event.preventDefault();$('#owner-files').click();}});
drop.addEventListener('dragover',event=>{event.preventDefault();drop.classList.add('drag');});drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));
drop.addEventListener('drop',event=>{event.preventDefault();drop.classList.remove('drag');uploadFiles(event.dataTransfer.files).catch(showError);});
$('#pause-transfer').addEventListener('click',()=>state.controller?.abort());
$('#cancel-transfer').addEventListener('click',async()=>{state.controller?.abort();if(!state.upload)return;try{const b=await request(prefix('/uploads/'+state.upload.id));await post('/uploads/'+b.id+'/cancel',{expected_revision:b.revision,reason:'대표 자료함에서 전송 취소'});$('#transfer-message').textContent='전송을 취소했습니다. 이미 접수된 원본은 삭제하지 않습니다.';await refresh();}catch(error){showError(error);}});
$('#refresh-documents').addEventListener('click',()=>refresh().catch(showError));$('#close-owner-dialog').addEventListener('click',()=>$('#owner-dialog').close());
setInterval(()=>{if(state.company && !document.hidden)refresh().catch(()=>{});},3000);
async function openLocalSession() {
  try {
    const result = await request('/api/login', {method:'POST', headers:{'content-type':'application/json'}, body:'{}'});
    await acceptSession(result);
  } catch {
    showLogin();
  }
}
request('/api/session').then(acceptSession).catch(() => { openLocalSession(); });
