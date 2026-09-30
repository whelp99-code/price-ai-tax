"""Internal book-account vocabulary, separate from statutory tax classifications."""
from typing import Literal

AccountCode = Literal[
    'cash','receivable','payable','revenue','expense','inventory','vat_input','vat_output','equity',
    'prepaid_expense','fixed_asset','accumulated_depreciation','depreciation_expense',
    'cost_of_sales','accrued_income','accrued_expense','payroll_expense','withholding_payable',
    'other_receivable','other_payable','asset_disposal_gain','asset_disposal_loss','local_withholding_payable',
    'transfer_clearing','card_payable'
]
ACCOUNT_INFO = {
    'cash':('현금·예금','asset'), 'receivable':('매출채권','asset'), 'payable':('매입채무','liability'),
    'revenue':('매출','income'), 'expense':('일반비용','expense'), 'inventory':('재고자산','asset'),
    'vat_input':('매입 부가세','asset'), 'vat_output':('매출 부가세','liability'), 'equity':('자본','equity'),
    'prepaid_expense':('선급비용','asset'), 'fixed_asset':('유형자산','asset'),
    'accumulated_depreciation':('감가상각누계액','contra_asset'), 'depreciation_expense':('감가상각비','expense'),
    'cost_of_sales':('매출원가','expense'), 'accrued_income':('미수수익','asset'),
    'accrued_expense':('미지급비용','liability'), 'payroll_expense':('급여','expense'),
    'withholding_payable':('원천징수 예수금','liability'), 'other_receivable':('기타채권','asset'),
    'other_payable':('기타채무','liability'), 'asset_disposal_gain':('자산처분이익','income'),
    'asset_disposal_loss':('자산처분손실','expense'),'local_withholding_payable':('지방소득세 예수금','liability'),
    'transfer_clearing':('계좌간 이체 대기','asset'),'card_payable':('카드 미지급금','liability')
}
EXPENSE_ACCOUNTS = frozenset(k for k,v in ACCOUNT_INFO.items() if v[1]=='expense')
INCOME_ACCOUNTS = frozenset(k for k,v in ACCOUNT_INFO.items() if v[1]=='income')
