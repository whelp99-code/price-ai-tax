"""Evidence-first release gate evaluator. Source presence never equals acceptance."""
from __future__ import annotations
import hashlib
import hmac
import json
from datetime import datetime,timezone
from pathlib import Path
from .db import digest
from .release_sources import runtime_source_manifest,unique_json_fields,reject_json_constant

VERSION_REQUIREMENTS={
 'V01':('owner_upload_e2e','chunk_recovery','company_scope','institution_original_bundle'),
 'V02':('parser_truth_matrix','scan_truth','encrypted_source','revision_replay'),
 'V03':('invoice_bank_dedup','card_lifecycle','internal_transfer','partial_settlement','actual_matching_truth'),
 'V04':('factual_question_flow','fact_history','live_model','assigned_professional_review'),
 'V05':('policy_independence','immutable_posting','real_policy_truth','controlled_activation'),
 'V06':('opening_balance','assets_inventory_accrual','consecutive_month_reconciliation','closing_source_coverage'),
 'V07':('vat_rules_verified','statutory_fields','form_validator','expert_vat_truth'),
 'V08':('payroll_withholding_rules','annual_adjustments','carryforward_truth','local_tax_rules','full_year_truth'),
 'V09':('live_source_sync','live_invoice_issue','live_tax_receipts','live_payment_confirmation','legal_authority'),
 'V10':('all_prior_versions','novice_acceptance','security_signoff','disaster_restore','thirty_day_observation','release_package')}
REAL_REQUIRED={'institution_original_bundle','parser_truth_matrix','scan_truth','actual_matching_truth','live_model','assigned_professional_review','real_policy_truth','consecutive_month_reconciliation','vat_rules_verified','form_validator','expert_vat_truth','payroll_withholding_rules','full_year_truth','local_tax_rules','live_source_sync','live_invoice_issue','live_tax_receipts','live_payment_confirmation','legal_authority','novice_acceptance','security_signoff','thirty_day_observation'}
LIVE_REQUIRED={'live_model','live_source_sync','live_invoice_issue','live_tax_receipts','live_payment_confirmation'}
CODE_COMPONENTS={
 'V01':['uploads.py','upload_models.py','upload_routes.py','owner.py','ingest_safety.py'],
 'V02':['source_tables.py','source_workbooks.py','source_xml.py','native_documents.py','extractions.py','source_html.py'],
 'V03':['casework.py','event_settlements.py'],
 'V04':['casework.py','review_cases.py','ai.py'],
 'V05':['autopolicies.py','policy_routes.py'],
 'V06':['finance_ops.py','service.py'],
 'V07':['tax_workpapers.py','filing.py'],
 'V08':['tax_workpapers.py'],
 'V09':['submission_gateway.py'],
 'V10':['release_readiness.py','release_sources.py','pipeline_health.py']}


def evaluate_evidence(records:list[dict])->dict:
    """Pure evaluator for independently validated release evidence supplied by ops.

    This function does not create evidence or attest its authenticity. Callers
    must bind signed reviewer identities and artifact hashes outside model input.
    """
    by_key={}
    for record in records:
        key=(record.get('version'),record.get('requirement'))
        by_key.setdefault(key,[]).append(record)
    versions=[]
    for version,requirements in VERSION_REQUIREMENTS.items():
        results=[]
        for requirement in requirements:
            if requirement=='all_prior_versions':
                ok=bool(versions) and all(item['accepted'] for item in versions)
                results.append({'requirement':requirement,'passed':ok,'reason':None if ok else 'EARLIER_VERSION_NOT_ACCEPTED'});continue
            options=by_key.get((version,requirement),[]);valid=[]
            for proof in options:
                if proof.get('state')!='VERIFIED' or proof.get('source_revision_current') is not True:continue
                if not isinstance(proof.get('artifact_sha256'),str) or len(proof['artifact_sha256'])!=64:continue
                if not all(c in '0123456789abcdef' for c in proof['artifact_sha256']):continue
                if not proof.get('reviewer') or proof.get('reviewer')==proof.get('author'):continue
                if proof.get('failures')!=0 or type(proof.get('failures')) is not int:continue
                if requirement in REAL_REQUIRED and proof.get('kind') not in ('REAL_FORMAT','LIVE','INDEPENDENT_SIGNOFF','USER_OBSERVATION'):continue
                if requirement in LIVE_REQUIRED and proof.get('kind')!='LIVE':continue
                if requirement=='thirty_day_observation' and (type(proof.get('observed_days')) is not int or proof['observed_days']<30):continue
                if requirement=='novice_acceptance' and (proof.get('account_switches')!=0 or proof.get('accounting_inputs')!=0 or type(proof.get('participants')) is not int or proof['participants']<5):continue
                valid.append(proof)
            results.append({'requirement':requirement,'passed':bool(valid),'reason':None if valid else 'MISSING_CURRENT_INDEPENDENT_EVIDENCE','proof_count':len(valid)})
        accepted=all(x['passed'] for x in results) and all(x['accepted'] for x in versions)
        versions.append({'version':version,'accepted':accepted,'state':'ACCEPTED' if accepted else 'NOT_ACCEPTED','requirements':results})
    return {'versions':versions,'final_version':'1.0.0','final_accepted':all(v['accepted'] for v in versions),'evidence_input_hash':digest(records)}


def component_inventory()->dict:
    root=Path(__file__).resolve().parent
    features={version:[{'file':'price/'+name,'present':(root/name).is_file(),'sha256':hashlib.sha256((root/name).read_bytes()).hexdigest() if (root/name).is_file() else None,'acceptance_implied':False} for name in names] for version,names in CODE_COMPONENTS.items()}
    features['_runtime_source_manifest']=runtime_source_manifest(root)
    return features


def verified_envelope(raw:bytes,key:bytes,component_hash:str)->list[dict]:
    if not 32<=len(key)<=128 or len(raw)>1_000_000:raise ValueError('RELEASE_EVIDENCE_SIZE')
    envelope=json.loads(raw,object_pairs_hook=unique_json_fields,parse_constant=reject_json_constant)
    if not isinstance(envelope,dict):raise ValueError('RELEASE_EVIDENCE_SCHEMA')
    if set(envelope)!={'schema','component_hash','expires_at','records','signature'}:raise ValueError('RELEASE_EVIDENCE_SCHEMA')
    if envelope['schema']!='price.signed-acceptance/1' or envelope['component_hash']!=component_hash:raise ValueError('RELEASE_CODE_CHANGED')
    expiry=datetime.fromisoformat(envelope['expires_at'])
    if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError('RELEASE_EVIDENCE_EXPIRED')
    if not isinstance(envelope['records'],list) or len(envelope['records'])>5000 or not all(isinstance(x,dict) for x in envelope['records']):raise ValueError('RELEASE_EVIDENCE_RECORDS')
    from .db import canonical
    payload={k:v for k,v in envelope.items() if k!='signature'}
    signature=hmac.new(key,canonical(payload).encode(),hashlib.sha256).hexdigest()
    if not isinstance(envelope['signature'],str) or not hmac.compare_digest(signature,envelope['signature']):raise ValueError('RELEASE_EVIDENCE_SIGNATURE')
    return envelope['records']


class ReleaseReadiness:
    def __init__(self,service):self.service,self.db,self.auth=service,service.db,service.auth
    def report(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            # No public endpoint accepts invented release evidence. Independently
            # signed evidence ingestion is an explicit remaining deployment task.
            evidence=[];inventory=component_inventory();status='NOT_PROVISIONED'
            root=self.db.path.parent
            manifest,key_file=root/'release-evidence.json',root/'release-evidence.key'
            if manifest.exists() and key_file.exists():
                try:
                    if manifest.is_symlink() or key_file.is_symlink() or key_file.stat().st_mode & 0o077:raise ValueError('RELEASE_KEY_PERMISSIONS')
                    if manifest.stat().st_size>1_000_000 or key_file.stat().st_size>128:raise ValueError('RELEASE_EVIDENCE_SIZE')
                    evidence=verified_envelope(manifest.read_bytes(),key_file.read_bytes(),digest(inventory));status='SIGNATURE_VERIFIED'
                except (ValueError,OSError,TypeError,KeyError):status='INVALID_OR_STALE'
            report=evaluate_evidence(evidence)
            report['components']=inventory
            report['evidence_records_loaded']=len(evidence)
            report['evidence_signature_status']=status
            report['independent_evidence_ingestion_configured']=status=='SIGNATURE_VERIFIED'
            report['notice']='코드와 합성 시험의 존재만으로 버전 인수를 선언하지 않습니다. 실제 자료·기관·전문 검토·운영 증거가 필요합니다.'
            return report
