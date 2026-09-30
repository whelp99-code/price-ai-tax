"""Recognize explicit account-balance exports; no guessed account mapping."""
import csv
import io
import re
import unicodedata
from decimal import Decimal
from .ingest_safety import text_decode
from .accounts import ACCOUNT_INFO

ALIASES={
 '현금':'cash','보통예금':'cash','당좌예금':'cash','현금및현금성자산':'cash',
 '매출채권':'receivable','외상매출금':'receivable','매입채무':'payable','외상매입금':'payable',
 '상품':'inventory','제품':'inventory','원재료':'inventory','재고자산':'inventory',
 '부가세대급금':'vat_input','부가세예수금':'vat_output','자본금':'equity','이익잉여금':'equity',
 '선급비용':'prepaid_expense','유형자산':'fixed_asset','비품':'fixed_asset','기계장치':'fixed_asset',
 '감가상각누계액':'accumulated_depreciation','미수수익':'accrued_income','미지급비용':'accrued_expense',
 '예수금':'withholding_payable','미수금':'other_receivable','미지급금':'other_payable'
}
ALIASES.update({k:k for k in ACCOUNT_INFO})


def try_parse_opening(raw:bytes):
    text,encoding=text_decode(raw)
    normalize=lambda x:re.sub(r'\s+','',unicodedata.normalize('NFKC',x)).replace('(원)','')
    found=None
    for delimiter in (',','\t',';'):
        try:
            for number,row in enumerate(csv.reader(io.StringIO(text),delimiter=delimiter,strict=True)):
                if number>40:break
                labels=[normalize(x) for x in row]
                if '계정과목' in labels and '차변잔액' in labels and '대변잔액' in labels:
                    if len(labels)!=len(set(labels)):return {'state':'VALIDATION_REQUIRED','records':[],'issues':[{'code':'OPENING_DUPLICATE_HEADER'}],'document_kind':'opening_balances'}
                    found=(delimiter,number,row,labels);break
        except csv.Error:continue
        if found:break
    if not found:return None
    from .source_tables import amount,date_value
    delimiter,header_index,header,labels=found
    prefix='\n'.join(text.splitlines()[:header_index])
    dates=list(dict.fromkeys(re.findall(r'(?:기준일|장부개시일)\s*[:：]\s*([0-9]{4}[-./][0-9]{1,2}[-./][0-9]{1,2})',prefix)))
    errors=[];when=None
    try:
        if len(dates)!=1:raise ValueError('date missing')
        when=date_value(dates[0])
    except ValueError:errors.append({'code':'OPENING_DATE_REQUIRED','row':0})
    currency='KRW' if re.search(r'(?:통화\s*[:：]\s*KRW|단위\s*[:：]\s*원)',prefix,re.I) or any('(원)' in x for x in header) else None
    if currency is None:errors.append({'code':'OPENING_CURRENCY_REQUIRED','row':0})
    indexes={field:labels.index(label) for field,label in [('account','계정과목'),('debit','차변잔액'),('credit','대변잔액')]}
    records=[];total_debit=total_credit=0;controls=[]
    reader=csv.reader(io.StringIO(text),delimiter=delimiter,strict=True)
    try:
        for n,row in enumerate(reader):
            if n<=header_index or not row or not any(x.strip() for x in row):continue
            line=reader.line_num
            if len(row)!=len(header):errors.append({'code':'COLUMN_COUNT_MISMATCH','row':line});continue
            if len(records)>1000:errors.append({'code':'OPENING_ROW_LIMIT','row':line});break
            original=row[indexes['account']].strip();account=ALIASES.get(normalize(original))
            try:
                debit=Decimal(amount(row[indexes['debit']],blank_zero=True));credit=Decimal(amount(row[indexes['credit']],blank_zero=True))
                if any(v<0 or v!=v.to_integral_value() or v>1_000_000_000_000 for v in (debit,credit)):raise ValueError('invalid money')
            except (ValueError,ArithmeticError):errors.append({'code':'OPENING_AMOUNT_INVALID','row':line});continue
            if normalize(original) in ('합계','총계'):
                controls.append({'row':line,'debit':int(debit),'credit':int(credit)});continue
            if account is None or ACCOUNT_INFO[account][1] in ('income','expense'):
                errors.append({'code':'OPENING_ACCOUNT_REVIEW','row':line,'source_account':original});continue
            if debit and credit:errors.append({'code':'OPENING_NET_BALANCE_REQUIRED','row':line});continue
            total_debit+=int(debit);total_credit+=int(credit)
            fields={key:{'row':line,'column':idx+1,'header':header[idx],'raw':row[idx]} for key,idx in indexes.items()}
            records.append({'row':line,'kind':'opening_balance','values':{'ledger_account':account,'debit':str(debit),'credit':str(credit),'date':when,'currency':currency,'source_account':original},'fields':fields,'context_pending':['independent_opening_review']})
    except csv.Error:errors.append({'code':'CSV_SYNTAX','row':reader.line_num})
    if not records or total_debit!=total_credit:errors.append({'code':'OPENING_UNBALANCED','row':0})
    for control in controls:
        if (control['debit'],control['credit'])!=(total_debit,total_credit):errors.append({'code':'OPENING_TOTAL_MISMATCH','row':control['row']})
    return {'state':'NORMALIZED' if not errors else 'VALIDATION_REQUIRED','document_kind':'opening_balances',
            'records':records,'record_count':len(records),'source_rows':len(records),'issues':errors,'controls':controls,
            'parser_version':'explicit-opening-balances/1','metadata':{'opening_date':when,'currency':currency},
            'encoding':encoding,'accounting_complete':False,'institution_verified':False,'statutory_validated':False}
