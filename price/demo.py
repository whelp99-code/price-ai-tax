"""Synthetic, opt-in fixture data. Never run against an initialized database."""
import base64
from .auth import Actor
from .models import CompanyInput, UserInput, TransactionInput, PeriodInput, AllocationInput
from .service import Service

DEMO_PASSWORD = "DemoOnly-Price-2026!"


def seed_demo(svc: Service):
    company, owner = svc.auth.bootstrap(CompanyInput(name="데모 법인 · 실제 회사 아님", industry="IT 서비스 / 합성 데이터"), "demo-owner", DEMO_PASSWORD, demo=True)
    reviewer_rec = svc.add_user(owner, company, UserInput(username="demo-reviewer", password=DEMO_PASSWORD, role="reviewer"))
    svc.add_user(owner, company, UserInput(username="demo-viewer", password=DEMO_PASSWORD, role="viewer"))
    reviewer = Actor(reviewer_rec["id"], "demo-reviewer")
    ev = svc.upload_evidence(owner, company, "합성거래_검증자료.txt", base64.b64encode("SYNTHETIC TEST ONLY. 실제 세금계산서/은행내역이 아닙니다. 매출 2,000,000 + 세액 200,000, 경비 300,000 + 세액 30,000.".encode()).decode())
    base = dict(event_date="2026-09-10", evidence_ids=[ev["id"]], source="manual", use_type="expense")
    sale = svc.create_transaction(owner, company, TransactionInput(**base, external_id="DEMO-SALE-001", direction="sale", counterparty="가상 고객 A", net=2_000_000, tax=200_000, gross=2_200_000, tax_category="standard", business_purpose="합성 IT 구축 매출"))
    purchase = svc.create_transaction(owner, company, TransactionInput(**base, external_id="DEMO-PURCHASE-001", direction="purchase", counterparty="가상 공급사 B", net=300_000, tax=30_000, gross=330_000, tax_category="standard", deductible=True, business_purpose="합성 업무용 구독료"))
    for tx in (sale, purchase):
        r = svc.analyze(owner, company, tx["id"])
        j = svc.transition_journal(reviewer, company, r["journal"]["id"], "approve", r["journal"]["revision"], "합성 시나리오 독립 검토")
        svc.transition_journal(owner, company, j["id"], "post", j["revision"], "데모 전표 확정")
    pending = svc.create_transaction(owner, company, TransactionInput(**base, external_id="DEMO-PURPOSE-001", direction="purchase", counterparty="확인 필요 · 장비 구매", net=500_000, tax=50_000, gross=550_000, tax_category="standard", deductible=None))
    svc.analyze(owner, company, pending["id"])
    unsupported = svc.create_transaction(owner, company, TransactionInput(**base, external_id="DEMO-FX-001", direction="purchase", counterparty="범위 밖 · 해외 공급사", currency="USD", net=30000, tax=0, gross=30000, tax_category="unknown", business_purpose="환산 기준 미확인"))
    svc.analyze(owner, company, unsupported["id"])
    bank = svc.create_transaction(owner, company, TransactionInput(external_id="DEMO-BANK-001", source="sample-bank", kind="bank", direction="receipt", event_date="2026-09-15", counterparty="가상 고객 A", net=2_200_000, gross=2_200_000, business_purpose="합성 매출 입금", evidence_ids=[ev["id"]]))
    svc.allocate(owner, company, AllocationInput(bank_id=bank["id"], invoice_id=sale["id"], amount=2_200_000, reason="합성 입금과 청구 비교"))
    r = svc.analyze(owner, company, bank["id"])
    j = svc.transition_journal(reviewer, company, r["journal"]["id"], "approve", r["journal"]["revision"], "합성 입금 검토")
    svc.transition_journal(owner, company, j["id"], "post", j["revision"])
    svc.create_period(owner, company, PeriodInput(label="2026년 9월 · 데모", start="2026-09-01", end="2026-09-30", required_sources=["manual", "sample-bank"]))
    return {"company_id": company, "demo": True, "users": ["demo-owner", "demo-reviewer", "demo-viewer"], "password": DEMO_PASSWORD, "warning": "공개 운영용 계정이 아닙니다. 데모 데이터는 실제 회사 자료가 아닙니다."}
