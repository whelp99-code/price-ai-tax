"""Close the two reproduced source-to-company validation gaps."""
from pathlib import Path
root = Path(__file__).resolve().parents[1]
path = root / 'price' / 'ingestion.py'
original = path.read_text(encoding='utf-8')
content = original
changes = [
    ('import json\n', 'import json\nimport re\n'),
    ('    def finish(self, job, result):\n', '''    def _ownership_conflicts(self, c, company, result):
        facts = self.db.list(c, company, 'company_facts')
        if not facts:
            return []
        profile = facts[0]
        normalize = lambda value: re.sub(r'[ -]', '', str(value or '')).upper()
        owned = {kind: {normalize(i['identifier']) for i in profile.get('instruments', []) if i['kind'] == kind} for kind in ('bank', 'card')}
        number = profile.get('business_number')
        problems = []
        for record in result.get('records', []):
            values = record['values']
            for kind, field in (('bank', 'account'), ('card', 'card')):
                ident = normalize(values.get(field))
                # Masked IDs are not strong enough to prove either ownership or a mismatch.
                if ident and owned[kind] and not any(x in ident for x in ('*', 'X')) and all(not any(ch in x for ch in ('*', 'X')) for x in owned[kind]) and ident not in owned[kind]:
                    problems.append({'row': record['row'], 'code': 'COMPANY_SOURCE_CONFLICT', 'field': field})
            parties = {normalize(values.get(field)) for field in ('supplier_id', 'buyer_id') if values.get(field)}
            if number and len(parties) == 2 and number not in parties:
                problems.append({'row': record['row'], 'code': 'COMPANY_SOURCE_CONFLICT', 'field': 'business_number'})
        return problems

    def finish(self, job, result):
'''),
    ("            if result.get('state') not in ('NORMALIZED', 'VALIDATION_REQUIRED', 'FORMAT_REVIEW', 'READ_FAILED', 'PERMISSION_REVIEW'):\n",
     "            conflicts = self._ownership_conflicts(c, job['company_id'], result) if result.get('state') == 'NORMALIZED' else []\n            if conflicts:\n                result = {**result, 'state': 'COMPANY_REVIEW', 'issues': conflicts + result.get('issues', [])}\n            if result.get('state') not in ('NORMALIZED', 'VALIDATION_REQUIRED', 'FORMAT_REVIEW', 'READ_FAILED', 'PERMISSION_REVIEW', 'COMPANY_REVIEW'):\n")
]
for before, after in changes:
    if content.count(before) != 1:
        raise RuntimeError('Exact-block precondition failed; source unchanged')
    content = content.replace(before, after, 1)
if path.read_text(encoding='utf-8') != original:
    raise RuntimeError('Concurrent change; source unchanged')
path.write_text(content, encoding='utf-8')
print('Company ownership checks applied before source case publication.')
