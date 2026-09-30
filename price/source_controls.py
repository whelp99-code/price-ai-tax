"""Arithmetic source controls; neither source completeness nor tax decisions."""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal


def bank_balance_checks(records):
    groups = defaultdict(list)
    for record in records:
        if record['kind'] == 'bank_statement':
            values = record['values']
            groups[(values.get('account'), values.get('currency'))].append(record)
    checks, issues = [], []
    for (account, currency), group in groups.items():
        dates = [r['values']['date'] for r in group]
        ascending = all(a <= b for a, b in zip(dates, dates[1:]))
        descending = all(a >= b for a, b in zip(dates, dates[1:]))
        summary = {'account': account, 'currency': currency, 'verified_pairs': 0,
                   'opening_balance_verified': False, 'source_complete': False, 'differences': []}
        if not ascending and not descending:
            summary['source_order'] = 'unordered'
            issues.append({'row': group[0]['row'], 'code': 'BANK_ORDER_REVIEW'})
            checks.append(summary)
            continue
        order = group if ascending else list(reversed(group))
        summary['source_order'] = 'ascending' if ascending else 'descending'
        for previous, current in zip(order, order[1:]):
            p, n = previous['values'], current['values']
            if p.get('balance') is None or n.get('balance') is None:
                continue
            difference = Decimal(p['balance']) + Decimal(n['credit']) - Decimal(n['debit']) - Decimal(n['balance'])
            if difference:
                summary['differences'].append({'row': current['row'], 'previous_row': previous['row'], 'difference': str(difference)})
                issues.append({'row': current['row'], 'code': 'RUNNING_BALANCE_MISMATCH', 'field': 'balance'})
            else:
                summary['verified_pairs'] += 1
        if summary['differences']:
            summary['status'] = 'MISMATCH'
        elif summary['verified_pairs']:
            summary['status'] = 'CHECKED_PAIRS_ONLY'
        else:
            summary['status'] = 'NOT_ENOUGH_BALANCES'
        checks.append(summary)
    return checks, issues


def total_currency_conflict(records):
    currencies = {r['values'].get('currency') for r in records}
    return len(currencies) > 1
