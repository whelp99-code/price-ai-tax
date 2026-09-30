'use strict';
// Uses authenticated owner-page helpers; no accounting or approval fields are collected.
let caseworkRenderToken=0;
async function renderBusinessFacts(){
  const token=++caseworkRenderToken,company=state.company;
  if(!company)return;
  try{
    const [questionData,eventData]=await Promise.all([request(prefix('/owner/questions')),request(prefix('/owner/events'))]);
    if(token!==caseworkRenderToken||company!==state.company)return;
    let root=document.querySelector('#event-fact-questions');
    if(!root){root=element('div');root.id='event-fact-questions';document.querySelector('#owner-questions').append(root);}
    root.replaceChildren();
    const count=questionData.questions.length+(state.home?.needs_company_confirmation?1:0);
    document.querySelector('#question-count').textContent=count?'('+count+')':'';
    for(const question of questionData.questions){
      const card=element('section',undefined,'panel');card.dataset.question=question.event_id+':'+question.key;
      card.append(element('p',(question.date||'날짜 확인 필요')+' · '+question.counterparty+' · '+(question.gross||'금액 확인 필요')+' '+(question.currency||''),'small'),element('h3',question.text));
      const choices=element('div',undefined,'row');
      for(const [value,label] of Object.entries(question.choices)){
        const choice=button(label,async()=>{
          for(const node of choices.querySelectorAll('button'))node.disabled=true;
          try{
            await post('/events/'+encodeURIComponent(question.event_id)+'/facts',{expected_revision:question.expected_revision,context_hash:question.context_hash,question:question.key,answer:value,idempotency_key:'fact_'+crypto.randomUUID()});
            toast(value==='unknown'?'추측하지 않고 필요한 검토 대상으로 남겼습니다.':'업무 사실을 저장하고 다음 확인을 이어갑니다.');
            await refresh();
          }finally{for(const node of choices.querySelectorAll('button'))node.disabled=false;}
        });
        choices.append(choice);
      }
      card.append(choices);root.append(card);
    }
    if(questionData.review_connection_needed)root.append(element('p',questionData.review_connection_needed+'개 거래는 자료 또는 전문 판단을 확인해야 합니다. 검토 담당자가 연결되지 않은 상태이며 완료로 처리하지 않습니다.','note'));
    if(questionData.source_changed)root.append(element('p','바뀐 자료의 거래 연결을 다시 확인하고 있습니다. 이전 질문은 사용하지 않습니다.','note'));
    const pending=document.querySelector('#pending-summary');
    let events=document.querySelector('#economic-event-summary');
    if(!events){events=element('div');events.id='economic-event-summary';pending.append(events);}
    events.replaceChildren();events.append(element('h3','자료에서 확인한 거래'));
    for(const event of eventData.events.slice(0,60)){
      const node=element('div',undefined,'note');
      const labels={NEEDS_FACTS:'업무 사실 확인 필요',FACTS_READY_ACCOUNTING_REVIEW:'사실 확인됨 · 회계 검토 대기',REVIEW_CONNECTION_NEEDED:'자료·검토 연결 필요',REVIEW_ASSIGNED:'검토 담당자 배정 · 수락 대기',IN_REVIEW:'담당자가 수락하여 검토 중',REVIEW_RESULT_PENDING_ACCOUNTING:'검토 결과 확인 · 회계 반영 전',SOURCE_CHANGED:'자료 변경 확인 중'};
      node.append(element('strong',event.display_counterparty+' · '+(event.values.gross||'금액 미확인')+' '+(event.values.currency||'')),element('p',labels[event.state]||'확인 필요'));
      if(Object.keys(event.facts||{}).length)node.append(element('p','확인한 사실 '+Object.keys(event.facts).length+'개 · 이 사실을 세무 승인으로 사용하지 않습니다.','small'));
      if(event.expert_assignee)node.append(element('p','담당자: '+event.expert_assignee.name+(event.expert_assignee.professional_credential_verified?'':' · 앱 검토 권한이며 세무 자격 검증은 별도입니다.'),'small'));
      events.append(node);
    }
    if(!eventData.events.length)events.append(element('p','자동 읽기 작업이 끝나면 원본과 연결된 거래를 표시합니다.'));
    if(eventData.events.length>60)events.append(element('p','앞의 60개 거래를 표시했습니다. 전체 거래는 원본과 함께 보존됩니다.','small'));
  }catch(error){if(company===state.company)showError(error);}
}
