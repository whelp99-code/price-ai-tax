"""Operational heartbeat and source-workflow health without private content.

A fresh heartbeat is evidence of a worker loop, not evidence of correct
accounting, completed filings, a system service or an Orca registration.
"""
from __future__ import annotations
import os
import time
from datetime import datetime,timezone
from .db import now,uid

HEALTH_SCHEMA='''CREATE TABLE IF NOT EXISTS worker_health(
 instance_id TEXT PRIMARY KEY, worker TEXT NOT NULL, pid INTEGER NOT NULL,
 started_at INTEGER NOT NULL, last_seen INTEGER NOT NULL, state TEXT NOT NULL,
 last_job_state TEXT, revision INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS worker_health_last_seen ON worker_health(worker,last_seen);'''


class WorkerHeartbeat:
    def __init__(self,db,*,clock=None,instance_id=None,worker_name='price'):
        if worker_name not in ('price','price-bookkeeping','price-automatic-books'):raise ValueError('Unknown worker heartbeat role')
        self.worker_name=worker_name
        self.db=db;self.clock=clock or time.time;self.instance_id=instance_id or uid('worker')
        self.started_at=int(self.clock());self.last_write=None
        with db.connect() as c:c.executescript(HEALTH_SCHEMA)

    def record(self,result=None,*,force=False):
        timestamp=int(self.clock())
        if not force and self.last_write is not None and timestamp-self.last_write<5:return False
        state=(result or {}).get('state')
        if state is not None:
            state=str(state)
            if len(state)>80 or not all(ch.isupper() or ch.isdigit() or ch=='_' for ch in state):state='REDACTED_STATE'
        with self.db.transaction() as c:
            c.execute('INSERT INTO worker_health(instance_id,worker,pid,started_at,last_seen,state,last_job_state) VALUES(?,?,?,?,?,?,?) ON CONFLICT(instance_id) DO UPDATE SET last_seen=excluded.last_seen,state=excluded.state,last_job_state=excluded.last_job_state,revision=worker_health.revision+1',
                      (self.instance_id,self.worker_name,os.getpid(),self.started_at,timestamp,'RUNNING',state))
            # Transport health is ephemeral, unlike immutable original evidence.
            c.execute('DELETE FROM worker_health WHERE last_seen<?',(timestamp-86400,))
        self.last_write=timestamp
        return True

    def stopped(self):
        with self.db.transaction() as c:c.execute("UPDATE worker_health SET state='STOPPED',last_seen=?,revision=revision+1 WHERE instance_id=?",(int(self.clock()),self.instance_id))


class PipelineHealth:
    def __init__(self,service,*,clock=None):
        self.db,self.auth=service.db,service.auth;self.clock=clock or time.time
        with self.db.connect() as c:c.executescript(HEALTH_SCHEMA)

    def report(self,actor,company):
        timestamp=int(self.clock());current=datetime.fromtimestamp(timestamp,timezone.utc)
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            c.execute('BEGIN')
            workers=[{'instance_id':r['instance_id'],'age_seconds':timestamp-r['last_seen'],'state':r['state'],'last_job_state':r['last_job_state']} for r in c.execute("SELECT * FROM worker_health WHERE worker='price' ORDER BY last_seen DESC LIMIT 20")]
            fresh=any(w['state']=='RUNNING' and 0<=w['age_seconds']<=180 for w in workers)
            book_rows=[dict(r) for r in c.execute("SELECT state,last_seen FROM worker_health WHERE worker='price-bookkeeping' ORDER BY last_seen DESC LIMIT 20")]
            book_fresh=any(r['state']=='RUNNING' and 0<=timestamp-r['last_seen']<=180 for r in book_rows)
            automatic_rows=[dict(r) for r in c.execute("SELECT state,last_seen FROM worker_health WHERE worker='price-automatic-books' ORDER BY last_seen DESC LIMIT 20")]
            automatic_fresh=any(r['state']=='RUNNING' and 0<=timestamp-r['last_seen']<=180 for r in automatic_rows)
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            queues={};alerts=[]
            if any(w['age_seconds']<0 for w in workers):alerts.append({'code':'WORKER_CLOCK_DIFFERENCE','message':'worker 기록과 현재 서버 시간이 다릅니다.'})
            for table in ('jobs','upload_jobs'):
                if table not in tables:continue
                counts={r['state']:r['count'] for r in c.execute(f'SELECT state,count(*) AS count FROM {table} WHERE company_id=? GROUP BY state',(company,))}
                queues[table]=counts
                stuck=c.execute(f"SELECT count(*) FROM {table} WHERE company_id=? AND state='RUNNING' AND lease_until<?",(company,timestamp)).fetchone()[0]
                if stuck:alerts.append({'code':'WORK_LEASE_EXPIRED','queue':table,'count':stuck,'message':'중단된 작업이 재처리를 기다립니다.'})
                if counts.get('FAILED',0):alerts.append({'code':'WORK_FAILED','queue':table,'count':counts['FAILED'],'message':'자료 처리 실패가 있습니다. 완료 상태로 표시하지 않습니다.'})
                if not fresh and (counts.get('QUEUED',0) or counts.get('RUNNING',0)):alerts.append({'code':'WORKER_NOT_RESPONDING','queue':table,'message':'대기 작업이 있으나 최근 worker 활동을 확인하지 못했습니다.'})
            review_counts={};overdue=0
            for review in self.db.list(c,company,'expert_review'):
                state=review['state'];review_counts[state]=review_counts.get(state,0)+1
                if state in ('ASSIGNED','IN_REVIEW') and review.get('due_at'):
                    due=datetime.fromisoformat(review['due_at'])
                    if due.tzinfo is not None and due<current:overdue+=1
            if review_counts.get('UNASSIGNED',0):alerts.append({'code':'REVIEW_UNASSIGNED','count':review_counts['UNASSIGNED'],'message':'담당자가 연결되지 않은 검토가 있습니다.'})
            if overdue:alerts.append({'code':'REVIEW_OVERDUE','count':overdue,'message':'약속한 검토 기한이 지났습니다.'})
            for package in self.db.list(c,company,'submission_package'):
                if package['state'] in ('SUBMITTING','OUTCOME_UNKNOWN'):
                    alerts.append({'code':'SUBMISSION_RECONCILIATION_REQUIRED','id':package['id'],'message':'이전 제출의 접수 여부를 먼저 확인해야 합니다. 재전송하지 않습니다.'})
            return {'checked_at':current.isoformat(),'worker_loop':{'responsive':fresh,'state':'RECENT_HEARTBEAT' if fresh else 'NO_RECENT_HEARTBEAT','instances':workers},
                    'finite_schedule_worker':{'responsive':book_fresh,'scope':'independently_approved_finite_book_schedules_only'},
                    'automatic_policy_worker':{'responsive':automatic_fresh,'scope':'reviewed_policies_and_owner_bounded_consent_only','release_acceptance_implied':False},
                    'queues':queues,'reviews':review_counts,'alerts':alerts,'external_notification_sent':False,
                    'financial_work_complete':False,'service_registration_verified':False,
                    'notice':'프로세스 활동과 작업 지연 상태입니다. 세무 판단 정확성이나 신고 완료를 뜻하지 않습니다.'}
