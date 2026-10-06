'use strict';
let discrepancyGeneration = 0;
let discrepancyToken = '';
const discrepancyFreshnessText = {
  STALE: '저장해 둔 원본 개정과 현재 저장된 같은 페이지 기록이 다릅니다. 해결된 것은 아닙니다.',
  STORED_UNCHANGED: '저장해 둔 개정 시각이 현재 저장 기록과 같습니다. 외부 원문은 다시 읽지 않았습니다.',
  UNKNOWN: '최신 여부는 확인하지 못했습니다. 없는 개정 시각이나 페이지 연결을 추측하지 않습니다.',
  CURRENT: '현재 저장된 원본과 업무 사실 기준입니다. 외부 원문은 다시 읽지 않았습니다.',
};
const discrepancyReviewReasonText = {
  FACT_CHANGED: '답변한 업무 사실이 바뀌어 이전 검토는 현재 내용에 대한 판단이 아닙니다.',
  SOURCE_CHANGED: '검토 후 원본 또는 회사 사실이 바뀌었습니다. 이전 판단은 현재 근거가 아닙니다.',
  SOURCE_INVALIDATED: '저장된 검토 상태가 원본 변경으로 무효가 되었습니다. 이전 판단은 현재 근거가 아닙니다.',
};
function discrepancyMoney(value) {
  if (!/^-?\d+$/.test(String(value ?? ''))) return '미확인';
  return BigInt(value).toLocaleString('ko-KR') + '원';
}
function discardDiscrepancySession() {
  discrepancyGeneration += 1;
  discrepancyToken = '';
  $('#discrepancy-inbox')?.remove();
}
function discrepancyRoot() {
  let root = $('#discrepancy-inbox');
  if (!root) {
    root = element('div', undefined, 'panel');
    root.id = 'discrepancy-inbox';
    $('#view-questions').append(root);
  }
  return root;
}
function discrepancyStillCurrent(generation, company, token) {
  return generation === discrepancyGeneration && company === state.company && token === state.csrf && token === discrepancyToken;
}
async function renderDiscrepancies() {
  const generation = ++discrepancyGeneration;
  const company = state.company;
  const token = state.csrf;
  discrepancyToken = token;
  const root = discrepancyRoot();
  root.replaceChildren(element('h2', '자료 불일치 개정 확인'));
  if (!company || !token) return;
  root.append(element('p', '저장된 개정 표시를 불러오는 중입니다.', 'small'));
  try {
    const result = await request(prefix('/discrepancies'));
    if (!discrepancyStillCurrent(generation, company, token)) return;
    root.replaceChildren(element('h2', '자료 불일치 개정 확인'), button('개정 확인 새로고침', renderDiscrepancies));
    root.append(element('p', '목록 조회는 기록을 바꾸지 않습니다. 재검토 요청은 검토 건을 만들고, 원본 변경 무효는 저장 상태를 바꿉니다.', 'note'));
    if (!result.items.length) root.append(element('p', '저장된 격리·전문 검토 기록이 없습니다. 전체 자료가 일치한다는 뜻은 아닙니다.', 'empty'));
    for (const item of result.items) {
      const card = element('details');
      card.dataset.discrepancy = item.id;
      card.dataset.kind = item.kind || '';
      card.dataset.state = item.state || '';
      card.dataset.sourceEvent = item.saved_id || '';
      if (item.freshness_reason) card.dataset.freshnessReason = item.freshness_reason;
      if (item.freshness) card.dataset.freshness = item.freshness;
      card.dataset.resolved = 'false';
      const summaryText = discrepancyReviewReasonText[item.freshness_reason]
        || discrepancyFreshnessText[item.freshness]
        || discrepancyFreshnessText.UNKNOWN;
      const summary = element('summary', summaryText);
      card.append(summary);
      card.append(element('p', '저장된 검토 상태: ' + (item.state || '없음'), 'small'));
      if (item.state === 'SOURCE_INVALIDATED') card.append(element('p', '저장된 검토 상태가 원본 변경으로 무효가 되었습니다. 이전 판단은 현재 근거가 아닙니다.', 'note'));
      if (item.kind === 'expert_review' && item.state !== 'SOURCE_INVALIDATED') {
        if (item.freshness_reason === 'FACT_CHANGED') {
          if (['owner', 'preparer'].includes(state.home?.company?.role)) {
            const action = button('바뀐 사실로 재검토 요청', async () => {
              try {
                await post('/expert-reviews', {
                  event_id: item.saved_id,
                  reason: '자료의 업무 사실이 바뀌어 재검토를 요청합니다.',
                  idempotency_key: crypto.randomUUID(),
                });
                await renderDiscrepancies();
              } catch (error) {
                const note = element('p', error.message, 'error');
                note.dataset.denial = 'rereview';
                card.append(note);
              }
            });
            action.dataset.rereview = item.id;
            card.append(action);
          }
        } else {
          const action = button('원본 변경이면 검토 무효', async () => {
            try {
              await post('/discrepancies/' + encodeURIComponent(item.id) + '/invalidate', {});
              await renderDiscrepancies();
            } catch (error) {
              const note = element('p', error.message, 'error');
              note.dataset.denial = 'invalidate';
              card.append(note);
            }
          });
          action.dataset.invalidate = item.id;
          card.append(action);
        }
      }
      if ((item.limitations || []).includes('same_page_saved_record_not_conflict_pair')) {
        card.append(element('p', '비교 대상은 같은 페이지의 현재 저장 기록입니다. 충돌 당시의 다른 원본으로 연결한 것이 아닙니다.', 'note'));
      }
      if ((item.limitations || []).includes('saved_source_missing')) {
        card.append(element('p', '같은 페이지의 저장 기록이 없습니다. 차액은 계산하지 않습니다.', 'note'));
      }
      if ((item.limitations || []).includes('zero_delta_not_resolved')) {
        card.append(element('p', '차액이 0원이어도 이 불일치가 해결된 것은 아닙니다.', 'note'));
      }
      const amounts = item.amounts;
      if (amounts && amounts.incoming) {
        const wrap = element('div', undefined, 'table-wrap');
        const table = element('table');
        const head = element('tr');
        for (const label of ['기록', '공급가', 'VAT', '합계']) head.append(element('th', label));
        table.append(head);
        const incoming = element('tr');
        incoming.append(element('th', '보류된 금액'));
        for (const key of ['net', 'tax', 'gross']) incoming.append(element('td', discrepancyMoney(amounts.incoming[key])));
        table.append(incoming);
        if (amounts.saved) {
          const saved = element('tr');
          saved.append(element('th', '같은 페이지 저장 기록'));
          for (const key of ['net', 'tax', 'gross']) saved.append(element('td', discrepancyMoney(amounts.saved[key])));
          table.append(saved);
        }
        if (item.differences) {
          const delta = element('tr');
          delta.append(element('th', '같은 페이지 차액 (해결 아님)'));
          for (const key of ['net', 'tax', 'gross']) delta.append(element('td', discrepancyMoney(item.differences[key])));
          table.append(delta);
        }
        wrap.append(table);
        card.append(wrap);
      }
      const technical = element('details');
      technical.append(element('summary', '근거 상세'));
      technical.append(element('p', '저장된 원문 사유: ' + (item.reason || '없음'), 'small'));
      technical.append(element('p', '기록 ID: ' + item.id + ' · 개정 ' + item.revision, 'small'));
      if (!item.professional_credential_verified) technical.append(element('p', '전문 자격 확인 안 됨', 'small'));
      card.append(technical);
      root.append(card);
    }
  } catch (error) {
    if (discrepancyStillCurrent(generation, company, token)) {
      root.replaceChildren(element('h2', '개정 확인 조회 실패'), element('p', error.message, 'error'), button('다시 조회', renderDiscrepancies));
    }
  }
}
document.querySelector('[data-view="questions"]').addEventListener('click', renderDiscrepancies);
$('#owner-company').addEventListener('change', () => {
  discardDiscrepancySession();
  if (!$('#view-questions').hidden) renderDiscrepancies();
});
