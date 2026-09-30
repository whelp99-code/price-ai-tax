from __future__ import annotations
import base64
import shutil
from dataclasses import dataclass
import pytest
from price.db import Database, canonical, now, uid
from price.auth import Actor
from price.service import Service
from price.models import *
from price.filing import Filings

PASSWORD = 'Test-Only-Price-2026!'

@dataclass
class World:
    db: Database
    svc: Service
    company: str
    owner: Actor
    reviewer: Actor
    preparer: Actor
    viewer: Actor
    other_company: str
    other_owner: Actor
    evidence: str = ''
    def tx(self, **changes):
        data = dict(external_id=uid('source'), direction='sale', event_date='2026-09-12',
                    counterparty='테스트 고객', net=1000, tax=100, gross=1100,
                    tax_category='standard', business_purpose='검증용 용역', evidence_ids=[self.evidence])
        data.update(changes)
        return self.svc.create_transaction(self.owner, self.company, TransactionInput(**data))
    def analyze(self, tx):
        return self.svc.analyze(self.owner, self.company, tx['id'])
    def post(self, tx):
        j = self.analyze(tx)['journal']
        j = self.svc.transition_journal(self.reviewer, self.company, j['id'], 'approve', j['revision'], '독립 검토')
        return self.svc.transition_journal(self.owner, self.company, j['id'], 'post', j['revision'])
    def period(self):
        return self.svc.create_period(self.owner, self.company, PeriodInput(label='2026년 9월', start='2026-09-01', end='2026-09-30'))
    def closed_period(self):
        p = self.period()
        p = self.svc.coverage(self.reviewer, self.company, p['id'], CoverageInput(expected_revision=p['revision'], opening_reviewed=True, sources_confirmed=['manual'], evidence_note='합성 자료의 기초잔액·수집 범위 수동 확인'))
        return self.svc.period_action(self.reviewer, self.company, p['id'], 'close', p['revision'], '합성 검증 마감')
    def ready_filing(self):
        self.post(self.tx())
        p = self.closed_period()
        calc = self.svc.calculate(self.owner, self.company, CalculationInput(period_id=p['id']))
        engine = Filings(self.svc)
        f = engine.create(self.owner, self.company, FilingInput(calculation_id=calc['id'], label='모의 신고'))
        f = engine.approve(self.reviewer, self.company, f['id'], f['revision'], '모의 신고 독립 검토')
        return engine, f, p

@pytest.fixture(scope='session')
def pristine(tmp_path_factory):
    root = tmp_path_factory.mktemp('base')
    db = Database(root/'source.sqlite3'); svc = Service(db)
    company, owner = svc.auth.bootstrap(CompanyInput(name='시험 회사 A'), 'test-owner', PASSWORD)
    actors = {}
    for role in ('reviewer','preparer','viewer'):
        r = svc.add_user(owner, company, UserInput(username='test-'+role, password=PASSWORD, role=role))
        actors[role] = Actor(r['id'], r['username'])
    other_company = uid('company')
    with db.transaction() as c:
        c.execute('INSERT INTO companies VALUES(?,?,?)', (other_company, canonical({'name':'시험 회사 B','demo':False,'ledger_mode':'validation'}), now()))
        r = svc.auth.add_user(c, other_company, UserInput(username='other-owner',password=PASSWORD,role='owner'))
        other_owner = Actor(r['id'], r['username'])
        db.audit(c, other_company, other_owner.id, 'company.initialized', other_company)
    backup = root/'pristine.sqlite3'; db.backup(backup)
    return backup, company, owner, actors, other_company, other_owner

@pytest.fixture
def w(tmp_path, pristine):
    backup, company, owner, actors, other_company, other_owner = pristine
    target = tmp_path/'price.sqlite3'; shutil.copyfile(backup, target)
    db=Database(target); svc=Service(db)
    world = World(db,svc,company,owner,actors['reviewer'],actors['preparer'],actors['viewer'],other_company,other_owner)
    ev=svc.upload_evidence(owner,company,'test.txt',base64.b64encode(b'SYNTHETIC ACCOUNTING EVIDENCE - NOT REAL TAX DATA').decode())
    world.evidence=ev['id']
    return world
