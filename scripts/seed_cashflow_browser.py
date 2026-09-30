"""Isolated synthetic setup for real card/transfer browser verification.

A new file is required. Never opens the existing demo or production database.
Synthetic invoice postings go through the existing test fixture's independently
reviewed policy service. No external account or financial action is involved.
"""
from __future__ import annotations
import base64,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from price.db import Database
from price.service import Service
from price.auth import Actor
from price.models import CompanyInput,UserInput
from conftest import World,PASSWORD
from test_card_books import setup as setup_card,bank
from test_internal_transfers import setup as setup_transfer


def world(service,prefix):
    company,owner=service.auth.bootstrap(CompanyInput(name='SYNTHETIC '+prefix+' COMPANY'),prefix+'-owner',PASSWORD)
    roles={}
    for role in ('reviewer','preparer','viewer'):
        user=service.add_user(owner,company,UserInput(username=prefix+'-'+role,password=PASSWORD,role=role));roles[role]=Actor(user['id'],user['username'])
    w=World(service.db,service,company,owner,roles['reviewer'],roles['preparer'],roles['viewer'],company,owner)
    original=service.upload_evidence(owner,company,'synthetic-proof.txt',base64.b64encode(b'SYNTHETIC SOURCE FOR ISOLATED BROWSER TEST ONLY').decode())
    w.evidence=original['id']
    return w


def main():
    path=Path(sys.argv[1]).resolve()
    if path.exists() or not path.is_relative_to(ROOT/'.data') or not path.parent.name.startswith('cashflow-browser-'):
        raise RuntimeError('Only a new isolated browser-fixture database is allowed')
    service=Service(Database(path))
    first=world(service,'cash-card')
    engine,invoice,card,posted=setup_card(first);engine.run_once(first.company)
    bank_event=bank(first)  # no receipt/card charge is posted yet; worker waits for approval
    transfer_db=path.with_name('transfers.sqlite3')
    if transfer_db.exists():raise RuntimeError('Separate transfer fixture already exists')
    second=world(Service(Database(transfer_db)),'cash-transfer');transfers,_=setup_transfer(second)
    info={'card_company':first.company,'transfer_company':second.company,'transfer_db':str(transfer_db),
          'invoice_journal':posted['id'],'card_event':card['id'],'bank_event':bank_event['id'],
          'card_plan':engine.list(first.owner,first.company)['items'][0]['id'],
          'transfer_plan':transfers.list(second.owner,second.company)['items'][0]['id'],
          'credential_kind':'known_test_fixture_password','synthetic':True}
    path.with_suffix('.fixture.json').write_text(json.dumps(info,ensure_ascii=False,indent=2))
    print(json.dumps(info))


if __name__=='__main__':main()
