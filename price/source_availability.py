"""Read-only view of which original files cover the requested source window.

This module never attests completeness, approves a period, or writes a record.
It shows printed query windows separately from the dates of individual rows.
"""
from __future__ import annotations
import json
from datetime import date,timedelta
from .db import DomainError
from .casework import normalized_id


def range_present(ranges,start,end):
    cursor=start
    for first,last in sorted((date.fromisoformat(r['start']),date.fromisoformat(r['end'])) for r in ranges):
        if last<cursor:continue
        if first>cursor:return False
        if last>=end:return True
        cursor=last+timedelta(days=1)
    return False


class SourceAvailability:
    def __init__(self,service):self.db,self.auth=service.db,service.auth

    def report(self,actor,company,start:date,end:date):
        if type(start) is not date or type(end) is not date or start>end or (end-start).days>366:
            raise DomainError('PERIOD_RANGE','조회할 기간을 1년 이내로 지정해 주세요.',422)
        with self.db.connect() as c:
            self.auth.guard(c,actor,company);c.execute('BEGIN')
            profiles=self.db.list(c,company,'company_facts');profile=profiles[0] if profiles else None
            instruments=profile.get('instruments',[]) if profile else []
            rows=c.execute("SELECT id,filename,state,revision,json_extract(result,'$.document_kind') AS kind,json_extract(result,'$.metadata') AS metadata,json_extract(result,'$.no_activity_declared') AS zero FROM upload_documents WHERE company_id=? ORDER BY created_at DESC LIMIT 501",(company,)).fetchall()
            truncated=len(rows)>500;documents=rows[:500];items=[];matched=set()
            groups=c.execute("SELECT json_extract(body,'$.document_id') AS document_id,json_extract(body,'$.kind') AS kind,json_extract(body,'$.values.account') AS account,json_extract(body,'$.values.card') AS card,min(json_extract(body,'$.values.date')) AS first,max(json_extract(body,'$.values.date')) AS last,count(*) AS rows FROM objects WHERE company_id=? AND kind='source_record' AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT' GROUP BY document_id,kind,account,card LIMIT 5001",(company,)).fetchall()
            truncated=truncated or len(groups)>5000;by_document={}
            for group in groups[:5000]:by_document.setdefault(group['document_id'],[]).append(dict(group))
            for instrument in instruments:
                kind=instrument['kind'];field='account' if kind=='bank' else 'card';identifier=normalized_id(instrument['identifier'])
                sources=[];ranges=[]
                for document in documents:
                    meta=json.loads(document['metadata']) if document['metadata'] else {}
                    candidates=[g for g in by_document.get(document['id'],[]) if normalized_id(g.get(field))==identifier]
                    meta_matches=normalized_id(meta.get(field))==identifier
                    if not candidates and not meta_matches:continue
                    interval=meta.get('declared_period')
                    valid_interval=interval and not (meta.get('declared_period_invalid') or meta.get('declared_period_conflict'))
                    dates=[g.get(k) for g in candidates for k in ('first','last') if g.get(k)]
                    intersects=valid_interval and interval['start']<=end.isoformat() and interval['end']>=start.isoformat()
                    observations=any(g.get('first') and g.get('last') and str(g['first'])[:10]<=end.isoformat() and str(g['last'])[:10]>=start.isoformat() for g in candidates)
                    if (dates or valid_interval) and not intersects and not observations:continue
                    matched.add(document['id'])
                    if valid_interval and document['state'] in ('NORMALIZED','NO_ACTIVITY_REPORTED'):ranges.append(interval)
                    sources.append({'document_id':document['id'],'filename':document['filename'],'state':document['state'],'source_revision':document['revision'],
                         'printed_query_period':interval if valid_interval else None,'row_count':sum(g['rows'] for g in candidates),
                         'first_observed':min(dates,default=None),'last_observed':max(dates,default=None),
                         'zero_activity_reported':bool(document['zero']),'completeness_verified':False})
                full_range=bool(ranges) and range_present(ranges,start,end)
                masked=any(ch in identifier for ch in ('X','*'))
                label=('계좌' if kind=='bank' else '카드')+' 끝자리 '+identifier[-4:]
                status='NO_FILES' if not sources else 'READING_OR_REVIEW' if any(s['state'] not in ('NORMALIZED','NO_ACTIVITY_REPORTED') for s in sources) else 'PRINTED_RANGE_PRESENT' if full_range else 'QUERY_RANGE_NOT_ESTABLISHED'
                if masked:status='MASKED_INSTRUMENT_REVIEW'
                request=(f'{label}의 {start.isoformat()}부터 {end.isoformat()}까지 조회한 원본을 올려주세요.' if status in ('NO_FILES','QUERY_RANGE_NOT_ESTABLISHED') else '원본의 조회 범위와 누락 여부를 내부 대조합니다.')
                items.append({'kind':kind,'instrument_label':label,'currency':instrument.get('currency'),'status':status,'sources':sources,
                              'printed_range_covers_request':full_range and not masked,'owner_request':request,'completeness_verified':False})
            unassigned=[{'document_id':r['id'],'filename':r['filename'],'state':r['state']} for r in documents if r['id'] not in matched and r['kind'] not in ('company_registration','opening_balances','opening_balance')]
            return {'period':{'start':start.isoformat(),'end':end.isoformat()},'instruments':items,'needs_company_facts':not bool(instruments),
                    'unassigned_documents':unassigned[:100],'unassigned_count_in_window_examined':len(unassigned),'partial_listing':truncated,
                    'completeness_verified':False,'period_closed':False,'financial_work_complete':False,
                    'notice':'조회기간이 표시된 파일과 실제 거래 날짜를 구분합니다. 자료가 있다는 사실만으로 누락 없음이나 마감 완료를 뜻하지 않습니다.'}
