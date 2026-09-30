'use strict';
const qs=s=>document.querySelector(s);
let session=null,company='',fetchSequence=0;
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(cls)el.className=cls;return el;}
function control(label,handler){const b=node('button',label,'quiet');b.type='button';b.addEventListener('click',async()=>{b.disabled=true;qs('#books-error').textContent='';try{await handler();}catch(e){qs('#books-error').textContent=e.message;}finally{b.disabled=false;}});return b;}
async function api(path,body){const options={credentials:'same-origin'};if(body!==undefined){options.method='POST';options.headers={'content-type':'application/json','x-csrf-token':session.csrf};options.body=JSON.stringify(body);}const result=await fetch('/api/companies/'+encodeURIComponent(company)+path,options);const value=await result.json();if(!result.ok)throw new Error(value.message||'요청을 처리하지 못했습니다.');return value;}
function reason(){const value=qs('#books-reason').value.trim();if(!value)throw new Error('검토 사유를 입력하세요.');return value;}
function section(title){const s=node('section',undefined,'panel');s.append(node('h2',title));qs('#books-content').append(s);return s;}
function detail(value){qs('#books-detail-text').textContent=JSON.stringify(value,null,2);qs('#books-details').showModal();}
function row(section,title,value,actions=[]){const div=node('div',undefined,'document'),left=node('div'),right=node('div',undefined,'actions');div.dataset.record=value.id;left.append(node('strong',title),node('p','상태: '+value.state));right.append(control('원본·계산 보기',()=>detail(value)));for(const button of actions)right.append(button);div.append(left,right);section.append(div);}
async function action(path,body){await api(path,body);await refresh();}
async function refresh(){if(!company)return;const request=++fetchSequence,selected=company;
 const [openings,schedules,payments,taxes,periods,journals,events]=await Promise.all([api('/opening-balances'),api('/book-schedules'),api('/payment-books'),api('/vat/returns'),api('/periods'),api('/journals'),api('/owner/events')]);if(request!==fetchSequence||selected!==company)return;
 qs('#books-content').replaceChildren();
 const role=session.companies.find(x=>x.id===company)?.role;
 const opening=section('기초잔액 원본 검토');if(!openings.length)opening.append(node('p','기초잔액 자료를 올리면 별도 입력 없이 검토 초안이 만들어집니다.'));
 for(const p of openings){const actions=[];if(p.state==='DRAFT')actions.push(control('독립 검토 승인',()=>action('/opening-balances/'+p.id+'/approve',{expected_revision:p.revision,reason:reason()})));if(p.state==='APPROVED')actions.push(control('검토된 기초잔액 반영',()=>action('/opening-balances/'+p.id+'/post',{})));row(opening,p.date+' · 기초잔액',p,actions);}
 const costs=section('선급비용·자산의 원장 일정');
 for(const p of schedules){const actions=[];if(p.state==='DRAFT')actions.push(control('일정 검토 승인',()=>action('/book-schedules/'+p.id+'/approve',{expected_revision:p.revision,reason:reason()})));if(p.state==='APPROVED')actions.push(control('도래한 일정 반영',()=>{const now=new Date();const through=now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');return action('/book-schedules/'+p.id+'/post-due',{through});}));row(costs,p.input.label,p,actions);}
 const choices=journals.filter(j=>j.state==='POSTED'&&!j.reversal_of&&j.lines.some(l=>l.account==='expense'&&l.side==='debit'));
 if(choices.length){const form=document.createElement('form');form.id='schedule-create';form.append(node('h3','원본 비용의 기간·자산 분류'));
 const select=document.createElement('select');select.name='source_journal_id';for(const j of choices){const o=node('option',j.event_date+' · '+j.description);o.value=j.id;select.append(o);}form.append(select);
 const type=document.createElement('select');type.name='kind';for(const [value,label] of [['prepaid','기간에 나누어 인식할 비용'],['asset','회사 자산']]){const o=node('option',label);o.value=value;type.append(o);}form.append(type);
 for(const [name,label,inputType] of [['label','설명','text'],['classification_date','분류일','date'],['start','사용 시작일','date'],['end','계약 종료일(기간 비용)','date'],['months','사용기간 개월(자산)','number'],['salvage','잔존금액(자산)','number']]){const l=node('label',label),input=document.createElement('input');input.name=name;input.type=inputType;if(name==='salvage')input.value='0';if(['label','start','classification_date'].includes(name))input.required=true;l.append(input);form.append(l);}
 const submit=control('원본 연결 일정 초안 생성',async()=>{if(!form.reportValidity())return;const data=Object.fromEntries(new FormData(form));const source=choices.find(x=>x.id===data.source_journal_id);data.evidence_ids=source.evidence_ids;data.idempotency_key=crypto.randomUUID();if(data.kind==='prepaid'){data.months=null;data.salvage=0;if(!data.end)throw new Error('계약 종료일이 필요합니다.');}else{data.end=null;data.months=Number(data.months);data.salvage=Number(data.salvage);}await action('/book-schedules',data);});form.append(submit);costs.append(form);}
 const pay=section('청구와 맞춘 대금 결제');
 for(const p of payments){const actions=[];if(p.state==='DRAFT')actions.push(control('결제 검토 승인',()=>action('/payment-books/'+p.id+'/approve',{expected_revision:p.revision,reason:reason()})));if(p.state==='APPROVED')actions.push(control('결제 장부 반영',()=>action('/payment-books/'+p.id+'/post',{})));row(pay,p.basis.date+' · '+p.basis.gross.toLocaleString('ko-KR')+'원',p,actions);}
 const eventRows=Array.isArray(events)?events:(events.events||events.items||[]);
 for(const e of eventRows.filter(e=>e.kind==='bank'&&e.lifecycle==='CURRENT'&&!payments.some(p=>p.basis.event_id===e.id&&['DRAFT','APPROVED','POSTED'].includes(p.state))).slice(0,30))pay.append(control(e.display_counterparty+' · 결제 초안 생성',()=>action('/payment-books',{payment_event_id:e.id})));
 pay.append(node('p','위 작업은 이미 발생한 입출금의 장부 기록입니다. 은행으로 송금을 요청하지 않습니다.','small'));
 const vat=section('부가세 원장·조정 검토표');
 for(const p of taxes){const actions=[];if(p.state==='DRAFT')actions.push(control('검토표 확인',()=>action('/vat/returns/'+p.id+'/review',{expected_revision:p.revision,reason:reason()})));row(vat,p.start+' ~ '+p.end,p,actions);}
 for(const period of periods)vat.append(control(period.label+' 검토표 작성',()=>action('/vat/returns',{period_id:period.id,taxpayer_kind:'corporate_general'})));
 vat.append(node('p','법정 신고서 전송·접수 화면이 아닙니다. 미해결 항목과 공식 서식 검증을 별도로 확인합니다.','small'));
}
qs('#books-refresh').addEventListener('click',()=>refresh().catch(e=>qs('#books-error').textContent=e.message));qs('#books-close').addEventListener('click',()=>qs('#books-details').close());
qs('#books-company').addEventListener('change',e=>{company=e.target.value;refresh().catch(e=>qs('#books-error').textContent=e.message);});
(async()=>{try{const response=await fetch('/api/session',{credentials:'same-origin'});if(!response.ok)throw new Error('로그인이 필요합니다. 대표 자료함 또는 기존 전문가 화면에서 본인 계정으로 로그인하세요.');session=await response.json();qs('#books-session-state').textContent=session.username+' · 각 실행의 권한과 독립성을 서버에서 재검증합니다.';for(const c of session.companies){const item=node('option',c.name);item.value=c.id;qs('#books-company').append(item);}company=session.companies[0]?.id||'';qs('#books-controls').hidden=!company;await refresh();}catch(e){qs('#books-session-state').textContent=e.message;}})();
