from datetime import datetime,timezone,timedelta
import pytest
from price.pipeline_health import WorkerHeartbeat,PipelineHealth
from price.db import DomainError
from test_source_ingestion import upload,BANK


def test_worker_liveness_is_not_financial_completion(w):
    clock=[2000000000]
    heartbeat=WorkerHeartbeat(w.db,clock=lambda:clock[0],instance_id='synthetic-worker')
    assert heartbeat.record(force=True)
    ops=PipelineHealth(w.svc,clock=lambda:clock[0])
    current=ops.report(w.owner,w.company)
    assert current['worker_loop']['responsive'] is True
    assert current['financial_work_complete'] is False and current['external_notification_sent'] is False
    assert heartbeat.record() is False
    clock[0]+=181
    assert ops.report(w.owner,w.company)['worker_loop']['responsive'] is False
    heartbeat.record({'state':'SUCCEEDED'},force=True)
    assert ops.report(w.owner,w.company)['worker_loop']['responsive'] is True
    heartbeat.stopped()
    assert ops.report(w.owner,w.company)['worker_loop']['responsive'] is False


def test_pending_sources_without_worker_generate_accurate_alert(w):
    upload(w,BANK)
    report=PipelineHealth(w.svc).report(w.owner,w.company)
    assert report['queues']['upload_jobs']['QUEUED']==1
    assert any(a['code']=='WORKER_NOT_RESPONDING' for a in report['alerts'])
    assert report['worker_loop']['responsive'] is False


def test_failed_and_expired_jobs_are_not_hidden(w):
    upload(w,BANK)
    with w.db.transaction() as c:c.execute("UPDATE upload_jobs SET state='RUNNING',lease_until=1")
    report=PipelineHealth(w.svc).report(w.owner,w.company)
    assert any(a['code']=='WORK_LEASE_EXPIRED' for a in report['alerts'])
    with w.db.transaction() as c:c.execute("UPDATE upload_jobs SET state='FAILED'")
    report=PipelineHealth(w.svc).report(w.owner,w.company)
    assert any(a['code']=='WORK_FAILED' for a in report['alerts'])


def test_heartbeat_does_not_store_result_content(w):
    heartbeat=WorkerHeartbeat(w.db,instance_id='synthetic-log')
    heartbeat.record({'state':'PRIVATE RESULT TEXT','document':'not allowed in health logs'},force=True)
    report=PipelineHealth(w.svc).report(w.owner,w.company)
    instance=report['worker_loop']['instances'][0]
    assert instance['last_job_state']=='REDACTED_STATE'
    assert 'document' not in str(report)
    with pytest.raises(DomainError):PipelineHealth(w.svc).report(w.other_owner,w.company)


def test_future_heartbeat_does_not_falsely_prove_liveness(w):
    heartbeat=WorkerHeartbeat(w.db,clock=lambda:2000000000,instance_id='future-synthetic')
    heartbeat.record(force=True)
    report=PipelineHealth(w.svc,clock=lambda:1999999000).report(w.owner,w.company)
    assert not report['worker_loop']['responsive']
    assert any(a['code']=='WORKER_CLOCK_DIFFERENCE' for a in report['alerts'])
