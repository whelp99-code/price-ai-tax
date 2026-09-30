"""Apply the reproduced late-company-fact invalidation fix."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
ingestion=root/'price/ingestion.py';owner=root/'price/owner.py'
a=ingestion.read_text(encoding='utf-8');b=owner.read_text(encoding='utf-8')
start=a.index('    def _ownership_conflicts(self, c, company, result):\n')
end=a.index('    def finish(self, job, result):\n',start)
old=a[start:end]
if 'return problems' not in old or 'masked' in old and len(old)>10000: raise RuntimeError('Unexpected identity implementation')
new='''    def _ownership_conflicts(self, c, company, result):
        from .source_identity import ownership_conflicts
        facts = self.db.list(c, company, 'company_facts')
        return ownership_conflicts(facts[0] if facts else None, result)

'''
after_a=a[:start]+new+a[end:]
needle="            self.db.audit(c, company, actor.id, 'company.facts_confirmed', result['id'], {'revision': result['revision'], 'source_document_id': data.source_document_id})\n            return result\n"
replacement="            self.db.audit(c, company, actor.id, 'company.facts_confirmed', result['id'], {'revision': result['revision'], 'source_document_id': data.source_document_id})\n            from .source_identity import hold_sources_on_fact_change\n            affected = hold_sources_on_fact_change(self.db, c, company, result, actor.id)\n            return {**result, 'source_context_held_ids': affected}\n"
if b.count(needle)!=1: raise RuntimeError('Expected company-facts block missing')
after_b=b.replace(needle,replacement,1)
if ingestion.read_text(encoding='utf-8')!=a or owner.read_text(encoding='utf-8')!=b: raise RuntimeError('Concurrent change')
ingestion.write_text(after_a,encoding='utf-8');owner.write_text(after_b,encoding='utf-8')
print('Late factual ownership rechecks connected. Original records and posted ledger preserved.')
