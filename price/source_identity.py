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
    retire_unbound_contexts(db, c, company, profile, actor_id)
    return affected
def normalize_identifier(value):
    return re.sub(r'[ -]', '', str(value or '')).upper()


def masked_identifier(value):
    return any(ch in str(value or '') for ch in ('*', 'X'))


def bank_records(records):
    return [record for record in records or [] if record.get('kind') == 'bank_statement']


def missing_bank_fields(records):
    missing = set()
    for record in bank_records(records):
        values = record.get('values') or {}
        if not normalize_identifier(values.get('account')):
            missing.add('account')
        if not values.get('currency'):
            missing.add('currency')
    return sorted(missing)


def context_conflicts(records, identifier, currency):
    problems = []
    accounts = set()
    for record in bank_records(records):
        values = record.get('values') or {}
        account = normalize_identifier(values.get('account'))
        if account:
            accounts.add(account)
            if masked_identifier(account):
                problems.append('masked_source_account')
            elif account != identifier:
                problems.append('account_conflict')
        current = values.get('currency')
        if current and current != currency:
            problems.append('currency_conflict')
    if len(accounts) > 1:
        problems.append('mixed_source_accounts')
    return sorted(set(problems))


def confirmed_bank_identifiers(profile):
    owned = set()
    for item in (profile or {}).get('instruments') or []:
        if item.get('kind') != 'bank':
            continue
        ident = normalize_identifier(item.get('identifier'))
        if ident and not masked_identifier(ident):
            owned.add(ident)
    return owned


def context_is_usable(confirmation, source_hash, generation, records):
    if not confirmation or not confirmation.get('active'):
        return False
    if confirmation.get('document_id') and records and confirmation.get('document_id') != records[0].get('document_id'):
        return False
    if confirmation.get('source_hash') != source_hash or int(confirmation.get('generation') or 0) != int(generation):
        return False
    if confirmation.get('basis') != 'owner_factual_statement' or confirmation.get('currency_origin') != 'owner_statement':
        return False
    if confirmation.get('instrument_currency_applied') or confirmation.get('applied_to_original'):
        return False
    if masked_identifier(confirmation.get('identifier')) or not confirmation.get('currency'):
        return False
    if context_conflicts(records, confirmation['identifier'], confirmation['currency']):
        return False
    return bool(missing_bank_fields(records))


def overlay_owner_context(record, confirmation):
    if record.get('kind') != 'bank_statement' or not confirmation or confirmation.get('document_id') != record.get('document_id'):
        return record
    if not context_is_usable(confirmation, record.get('source_hash'), record.get('generation') or 1, [record]):
        return record
    values = dict(record.get('values') or {})
    applied = []
    if not normalize_identifier(values.get('account')):
        values['account'] = confirmation['identifier']
        applied.append('account')
    if not values.get('currency'):
        values['currency'] = confirmation['currency']
        applied.append('currency')
    if not applied:
        return record
    return {**record, 'values': values, 'source_values': dict(record.get('values') or {}), 'context_provenance': {
        'basis': 'owner_factual_statement', 'confirmation_id': confirmation['id'], 'confirmed_by': confirmation.get('confirmed_by'),
        'revision': confirmation.get('revision'), 'fields': applied, 'currency_origin': 'owner_statement',
        'instrument_currency_applied': False, 'original_preserved': True}}


def _deactivate_context(db, c, company, ctx, actor_id, reason):
    updated = {k: v for k, v in ctx.items() if k not in ('id', 'revision', 'created_at', 'updated_at')}
    updated.update(active=False, invalidation_reason=reason, invalidated_by=actor_id, applied_to_original=False)
    saved = db.replace(c, company, 'document_source_context', ctx['id'], updated, ctx['revision'])
    db.audit(c, company, actor_id, 'document.context_invalidated', ctx['document_id'], {
        'confirmation_id': ctx['id'], 'reason': reason, 'revision': saved['revision'], 'original_preserved': True})
    return saved


def retire_document_contexts(db, c, company, document_id, source_hash, generation, actor_id):
    retired = []
    for ctx in db.list(c, company, 'document_source_context'):
        if ctx.get('document_id') != document_id or not ctx.get('active'):
            continue
        if ctx.get('source_hash') == source_hash and int(ctx.get('generation') or 0) == int(generation):
            continue
        _deactivate_context(db, c, company, ctx, actor_id, 'source_generation')
        retired.append(ctx['id'])
    return retired


def retire_unbound_contexts(db, c, company, profile, actor_id):
    owned = confirmed_bank_identifiers(profile)
    retired = []
    for ctx in db.list(c, company, 'document_source_context'):
        if not ctx.get('active'):
            continue
        if ctx.get('identifier') in owned and not masked_identifier(ctx.get('identifier')):
            continue
        _deactivate_context(db, c, company, ctx, actor_id, 'instrument_conflict')
        retired.append(ctx['id'])
    return retired
