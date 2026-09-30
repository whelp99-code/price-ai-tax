"""Pure factual source-to-company constraints, not a legal identity check."""
from __future__ import annotations
import re


def ownership_conflicts(profile, result):
    if not profile:
        return []
    normalize=lambda value: re.sub(r'[ -]','',str(value or '')).upper()
    owned={kind:{normalize(i['identifier']) for i in profile.get('instruments',[]) if i['kind']==kind} for kind in ('bank','card')}
    number=profile.get('business_number')
    problems=[]
    for record in result.get('records',[]):
        values=record['values']
        for kind,field in (('bank','account'),('card','card')):
            ident=normalize(values.get(field))
            if ident and owned[kind] and not any(x in ident for x in ('*','X')) and all(not any(ch in x for ch in ('*','X')) for x in owned[kind]) and ident not in owned[kind]:
                problems.append({'row':record['row'],'code':'COMPANY_SOURCE_CONFLICT','field':field})
        parties={normalize(values.get(field)) for field in ('supplier_id','buyer_id') if values.get(field)}
        if number and len(parties)==2 and number not in parties:
            problems.append({'row':record['row'],'code':'COMPANY_SOURCE_CONFLICT','field':'business_number'})
        if record.get('kind')=='company_registration' and number and values.get('business_number') and number!=values['business_number']:
            problems.append({'row':record['row'],'code':'COMPANY_SOURCE_CONFLICT','field':'business_number'})
    return problems


def hold_sources_on_fact_change(db,c,company,profile,actor_id):
    """Recheck already-read sources when ownership facts arrive later.

    Previously stored candidate records remain in history. No posted journal
    is touched; this is a source-readiness invalidation, not a correction.
    """
    import json
    from .db import canonical,now
    affected=[]
    documents=c.execute("SELECT id,result FROM upload_documents WHERE company_id=? AND state='NORMALIZED'",(company,)).fetchall()
    for document in documents:
        records=[db.unpack(row) for row in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT'",(company,document['id']))]
        conflicts=ownership_conflicts(profile,{'records':records})
        if not conflicts:
            continue
        result=json.loads(document['result']) if document['result'] else {}
        result['issues']=conflicts+result.get('issues',[])
        result['ownership_revision']=profile['revision']
        c.execute("UPDATE upload_documents SET state='COMPANY_REVIEW',result=?,revision=revision+1,updated_at=? WHERE company_id=? AND id=?",(canonical(result),now(),company,document['id']))
        for row in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='ingestion_case' AND json_extract(body,'$.document_id')=?",(company,document['id'])).fetchall():
            case=db.unpack(row)
            db.replace(c,company,'ingestion_case',case['id'],{**case,'state':'CONTEXT_REVIEW','reason':'나중에 확인된 회사 정보와 원본이 맞지 않아 거래 연결을 보류합니다.','company_revision':profile['revision']},case['revision'])
        db.audit(c,company,actor_id,'source.company_context_invalidated',document['id'],{'company_revision':profile['revision'],'issue_count':len(conflicts)})
        affected.append(document['id'])
    return affected
