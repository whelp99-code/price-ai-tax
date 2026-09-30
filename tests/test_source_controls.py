from __future__ import annotations
from price.source_tables import parse_source_table
from test_source_ingestion import BANK


def test_running_balance_mismatch_is_not_normalized():
    raw=BANK.replace(b',220,880',b',220,881')
    result=parse_source_table(raw)
    assert result['state']=='VALIDATION_REQUIRED'
    assert any(x['code']=='RUNNING_BALANCE_MISMATCH' for x in result['issues'])


def test_ascending_and_descending_source_order_balance_checks():
    a=parse_source_table(BANK)
    lines=BANK.decode().splitlines()
    b=parse_source_table(('\n'.join(lines[:4]+list(reversed(lines[4:6]))+lines[6:])+'\n').encode())
    assert a['state']==b['state']=='NORMALIZED'
    assert a['balance_checks'][0]['verified_pairs']==b['balance_checks'][0]['verified_pairs']==1
    assert a['balance_checks'][0]['source_order']=='ascending'
    assert b['balance_checks'][0]['source_order']=='descending'
    assert a['balance_checks'][0]['opening_balance_verified'] is False


def test_different_accounts_are_not_balance_chained():
    raw=('거래일시,거래내용,입금액,출금액,잔액,계좌번호,통화\n'
         '2026-09-01 10:00:00,A,100,0,100,111111111,KRW\n'
         '2026-09-01 11:00:00,B,200,0,200,222222222,KRW\n').encode()
    result=parse_source_table(raw)
    assert result['state']=='NORMALIZED'
    assert all(x['verified_pairs']==0 for x in result['balance_checks'])


def test_mixed_currency_grand_total_is_not_meaningful():
    raw=('승인일자,카드번호,가맹점명,승인금액,승인번호,통화\n'
         '2026-09-01,1111111111111111,A,100,A1,KRW\n'
         '2026-09-02,1111111111111111,B,200,A2,USD\n'
         '합계,,,300,,\n').encode()
    result=parse_source_table(raw)
    assert result['state']=='VALIDATION_REQUIRED'
    assert any(x['code']=='CONTROL_CURRENCY_AMBIGUOUS' for x in result['issues'])


def test_card_cancellation_keeps_signed_amount_not_new_charge():
    raw=('승인일자,카드번호,가맹점명,승인금액,승인번호,승인취소구분,통화\n'
         '2026-09-01,1111111111111111,A,-100,A1,취소,KRW\n').encode()
    result=parse_source_table(raw)
    assert result['state']=='NORMALIZED' and result['records'][0]['values']['gross']=='-100'


def test_source_with_no_balance_cannot_claim_verified_balance():
    raw=('거래일시,거래내용,입금액,출금액,통화\n'
         '2026-09-01,A,100,0,KRW\n'
         '2026-09-02,B,0,20,KRW\n').encode()
    result=parse_source_table(raw)
    assert result['state']=='NORMALIZED'
    assert not any(x['verified_pairs'] for x in result['balance_checks'])


def test_out_of_order_dates_are_explicit_not_arbitrary_sorted_balance():
    raw=('거래일시,거래내용,입금액,출금액,잔액,통화\n'
         '2026-09-02,A,100,0,100,KRW\n'
         '2026-09-01,B,0,20,80,KRW\n'
         '2026-09-03,C,10,0,90,KRW\n').encode()
    result=parse_source_table(raw)
    assert result['state']=='VALIDATION_REQUIRED'
    assert any(x['code']=='BANK_ORDER_REVIEW' for x in result['issues'])
