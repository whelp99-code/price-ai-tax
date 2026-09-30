from __future__ import annotations
import pytest
from price.extractions import Extractions,ReprocessInput
from price.db import DomainError
from test_extraction_revisions import ready


def test_history_is_paginated_without_repeating_entire_source_values(w):
    reader,doc=ready(w);history=Extractions(w.svc)
    for index in range(2):
        current=reader.uploads.document(w.owner,w.company,doc['id'])
        history.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=current['revision'],idempotency_key='history-replay-'+str(index),reason='변경 이력 시험'))
        reader.run_once(w.company)
    first=history.history(w.owner,w.company,doc['id'],offset=0,limit=2)
    second=history.history(w.owner,w.company,doc['id'],offset=2,limit=2)
    assert first['total']==second['total']==3 and first['next_offset']==2
    assert second['next_offset'] is None
    assert [r['generation'] for r in first['runs']+second['runs']]==[1,2,3]
    assert all('record_values' not in r and 'record_ids' not in r for r in first['runs'])
    assert first['runs'][0]['record_count']==2


@pytest.mark.parametrize('offset,limit',[(-1,10),(0,0),(0,101),(True,10),(0,True),(0,1.5)])
def test_history_rejects_unbounded_or_noninteger_pages(w,offset,limit):
    reader,doc=ready(w)
    with pytest.raises(DomainError):Extractions(w.svc).history(w.owner,w.company,doc['id'],offset=offset,limit=limit)
