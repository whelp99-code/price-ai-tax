'use strict';
/* Owner-only language: no account selection, tax classification or self-approval. */
let bookResultsGeneration=0;
function money(value){return value===null||value===undefined?'자료 확인 필요':Number(value).toLocaleString('ko-KR')+'원';}
function bookPanel(id,title){let target=document.getElementById(id);if(!target){target=element('div',undefined,'panel');target.id=id;$('#view-results').append(target);}target.replaceChildren(element('h2',title));return target;}
async function renderBookResults(){
 if(!state.company||$('#view-results').hidden)return;
 const company=state.company,generation=++bookResultsGeneration;
 let controls=document.getElementById('book-period-controls');
 if(!controls){controls=element('div',undefined,'panel');controls.id='book-period-controls';controls.append(element('h2','회사 돈과 비용을 확인하세요.'));const form=document.createElement('form');form.id='book-period-form';form.style.display='flex';form.style.flexWrap='wrap';form.style.alignItems='end';
 const label=element('label','확인할 달');const month=document.createElement('input');month.type='month';month.id='book-month';month.required=true;const today=new Date();month.value=today.getFullYear()+'-'+String(today.getMonth()+1).padStart(2,'0');label.append(month);form.append(label,button('결과 새로고침',renderBookResults,'primary'));form.addEventListener('submit',e=>{e.preventDefault();renderBookResults().catch(showError);});controls.append(form,element('p','확정된 장부를 기준으로 표시합니다. 아직 읽지 못한 자료나 기초잔액은 숨기지 않습니다.','small'));$('#view-results').append(controls);}
 const period=$('#book-month').value;if(!/^\d{4}-\d{2}$/.test(period))return;
 const [year,month]=period.split('-').map(Number),start=period+'-01',end=period+'-'+new Date(year,month,0).getDate();
 const cards=bookPanel('owner-money-report','입출금과 손익');cards.append(element('p','장부를 확인하고 있습니다.'));
 try{
 const [report,plans,stock,tax,automation,cardLedger,transfers,availability]=await Promise.all([request(prefix('/owner/financial-statements?start='+start+'&end='+end)),request(prefix('/owner/book-schedules')),request(prefix('/owner/inventory')),request(prefix('/owner/tax-summary')),request(prefix('/owner/automation')),request(prefix('/owner/card-books')),request(prefix('/owner/internal-transfers')),request(prefix('/owner/source-availability?start='+start+'&end='+end))]);
 if(company!==state.company||generation!==bookResultsGeneration)return;
 cards.replaceChildren(element('h2','입출금과 손익'));
 const stats=element('div',undefined,'stats');
 for(const [label,value] of [['들어온 돈',report.cash.received],['나간 돈',report.cash.paid],['남아 있는 돈',report.cash.closing],['매출·기타수익',report.income_statement.income],['사용된 비용',report.income_statement.expenses],['장부상 이익',report.income_statement.net_profit]]){const card=element('div',undefined,'stat');card.append(element('span',label),element('strong',money(value)));stats.append(card);}cards.append(stats);
 if(report.blockers.length){cards.append(element('p','아직 '+report.blockers.length+'개의 확인사항이 있어 잠정 결과입니다.','note'));for(const blocker of report.blockers.slice(0,5))cards.append(element('p',blocker.message||'원본과 장부 연결을 확인하고 있습니다.','small'));}
 cards.append(element('p','통장에 들어온 금액과 매출은 다를 수 있습니다. 기초자료가 미확인일 때 잔액을 0원으로 표시하지 않습니다.','small'));
 const available=bookPanel('owner-source-availability','올린 자료의 조회 범위');
 if(availability.needs_company_facts)available.append(element('p','먼저 자료함의 회사 계좌·카드 정보를 사실 확인해 주세요.'));
 for(const item of availability.instruments){const messages={NO_FILES:'아직 원본 없음',QUERY_RANGE_NOT_ESTABLISHED:'조회기간 확인 필요',READING_OR_REVIEW:'자료 읽기·확인 대기',PRINTED_RANGE_PRESENT:'전체 조회기간이 표시된 자료 있음',MASKED_INSTRUMENT_REVIEW:'마스킹된 계좌·카드 구분 필요'};available.append(element('strong',item.instrument_label+' · '+messages[item.status]),element('p',item.owner_request,'small'));}
 if(availability.partial_listing)available.append(element('p','표시 한도를 넘는 자료가 있어 이 목록은 일부입니다.','note'));
 available.append(element('p',availability.notice,'small'));
 const automatic=bookPanel('owner-automatic-report','자료에서 장부까지 자동 처리');
 automatic.append(element('p','독립 검토된 규칙으로 장부에 반영한 거래 '+automation.posted_events+'건 · '+money(automation.posted_gross)));
 automatic.append(element('p','한 번 정한 기간·금액·건수 안에서만 처리합니다. 세금 분류나 계정과목을 선택하는 절차가 아닙니다.','small'));
 if(!automation.grants.some(g=>g.state==='ACTIVE'))automatic.append(element('p','아직 활성화된 자동 처리 동의가 없습니다. 전문가가 규칙을 검토한 뒤 처리 범위를 확인합니다.','note'));
 for(const grant of automation.grants.filter(g=>g.state==='ACTIVE')){automatic.append(element('p',grant.spec.period_start+' ~ '+grant.spec.period_end+' · 처리 '+grant.processed_events+'/'+grant.spec.max_events+'건 · 총 한도 '+money(grant.spec.max_total),'note'));
 if(state.home?.company.role==='owner')automatic.append(button('자동 처리 중지',async()=>{await post('/automation/consents/'+grant.id+'/stop',{expected_revision:grant.revision,reason:'대표 화면에서 자동 처리 중지'});await renderBookResults();}));}
 for(const held of automation.held.slice(0,5))automatic.append(element('p',held.message,'small'));
 automatic.append(element('p','동의 저장과 실행기 가동은 별도입니다. 실제 회사 자동기장 배포는 운영 인수 전이며, 이미 반영된 장부와 미처리를 구분합니다.','small'));
 const cardPanel=bookPanel('owner-card-report','카드 사용과 대금 납부');
 const labels={invoice_charge:'카드로 지급한 구매 대금',cancellation:'원승인 취소',billing:'납부·환급 대사'};
 for(const [instrument,balance] of Object.entries(cardLedger.balances))cardPanel.append(element('p','카드 끝자리 '+instrument.slice(-4)+' · '+(balance>=0?'아직 정산할 장부금액 ':'환급 대기 장부금액 ')+money(Math.abs(balance)),'note'));
 for(const p of cardLedger.items.filter(p=>p.date>=start&&p.date<=end).slice(0,20))cardPanel.append(element('p',p.date+' · '+labels[p.operation]+' '+money(p.amount)+' · '+(p.source_review_needed?'원본 재확인 필요':p.state==='POSTED'?'장부 반영됨':p.state==='APPROVED'?'검토됨·반영 대기':'내부 확인 필요')));
 if(!cardLedger.items.length)cardPanel.append(element('p','카드와 원매입·통장 자료의 참조가 확인되면 연결됩니다. 카드 잔액이 0원으로 확인됐다는 뜻은 아닙니다.'));
 cardPanel.append(element('p',cardLedger.notice,'small'));
 const transferPanel=bookPanel('owner-transfer-report','회사 계좌 사이 이동');
 for(const t of transfers.items.filter(t=>t.date>=start&&t.date<=end).slice(0,20))transferPanel.append(element('p',t.date+' · 끝자리 '+t.from_account.slice(-4)+' → '+t.to_account.slice(-4)+' · '+money(t.amount)+' · '+(t.state==='POSTED'?'양쪽 장부 반영됨':t.state==='APPROVED'?'검토됨·반영 대기':t.state==='SOURCE_CHANGED'?'원본 재확인 필요':'내부 확인 필요'),'note'));
 if(!transfers.items.length)transferPanel.append(element('p','서로 대응하는 회사 계좌 이체가 아직 확인되지 않았습니다.'));
 transferPanel.append(element('p','회사 계좌 사이 이동은 외부 수입·지출이나 새 매출·비용으로 더하지 않습니다. 은행 송금 기능이 아닙니다.','small'));
 const schedule=bookPanel('owner-cost-report','미리 낸 비용과 회사 자산');
 if(!plans.items.length)schedule.append(element('p','아직 검토된 비용·자산 일정이 없습니다. 계약·구매 자료를 함께 올려주세요.'));
 for(const p of plans.items){const row=element('div',undefined,'document');const content=element('div');content.append(element('strong',p.label),element('p','원금 '+money(p.original_cost)+' · 이미 비용에 반영 '+money(p.recognized_expense)+' · 남은 장부금액 '+money(p.remaining_value)));row.append(content,element('span',p.correction_needed?'정정 확인 필요':p.state==='COMPLETE'?'일정 반영 완료':p.state==='APPROVED'?'검토 완료·일정 반영 중':p.state==='CANCELLED'?'후속 반영 중단':'전문 검토 필요','pill'));schedule.append(row);}
 schedule.append(element('p',plans.notice,'small'));
 const inventory=bookPanel('owner-stock-report','아직 보유한 물품');
 if(!stock.items.length)inventory.append(element('p','확인된 재고 항목이 없습니다. 재고가 0개로 확정됐다는 뜻은 아닙니다.'));
 for(const item of stock.items)inventory.append(element('p',item.sku+' · '+item.quantity.toLocaleString('ko-KR')+'개 · 장부금액 '+money(item.value)+(item.source_review_needed?' · 원본 확인 필요':''),'note'));
 inventory.append(element('p',stock.notice,'small'));
 const taxes=bookPanel('owner-vat-report','부가세 검토 결과');
 const matching=tax.items.filter(r=>r.start<=end&&r.end>=start).reverse();
 if(!matching.length)taxes.append(element('p','선택한 기간의 부가세 검토표가 아직 작성되지 않았습니다. 신고·납부는 실행되지 않았습니다.'));
 for(const item of matching.slice(0,5)){const signed=item.estimated_tax_due_or_refund;taxes.append(element('strong',item.start+' ~ '+item.end),element('p',(signed<0?'예상 환급 검토금액 ':'예상 납부 검토금액 ')+money(Math.abs(signed)),'note'),element('p',(item.state==='REVIEWED_WORKPAPER'?'검토 확인':'초안')+' · 미해결 '+item.unresolved_count+'건 · 실제 신고 미접수','small'));}
 }catch(error){if(company===state.company&&generation===bookResultsGeneration){cards.replaceChildren(element('h2','결과 확인 필요'),element('p',error.message,'error'));}}
}
for(const tab of document.querySelectorAll('[data-view]'))tab.addEventListener('click',()=>{if(tab.dataset.view==='results')renderBookResults().catch(showError);});
$('#owner-company').addEventListener('change',()=>{bookResultsGeneration++;for(const id of ['owner-money-report','owner-cost-report','owner-stock-report','owner-vat-report','owner-automatic-report','owner-card-report','owner-transfer-report','owner-source-availability'])document.getElementById(id)?.remove();if(!$('#view-results').hidden)renderBookResults().catch(showError);});
