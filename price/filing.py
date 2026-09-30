"""Internal review packages and an explicitly labelled local filing simulator."""
from __future__ import annotations
import time
from .auth import Actor
from .db import DomainError, digest, uid
from .service import WRITERS, REVIEWERS
from .models import FilingInput, SubmitInput, PaymentInput


class Filings:
    def __init__(self, service):
        self.svc, self.db, self.auth = service, service.db, service.auth

    def _basis(self, c, company, f):
        calc = self.db.get(c, company, "calculation", f["calculation_id"])
        p = self.db.get(c, company, "period", calc["period_id"])
        if calc["dependency_hash"] != self.svc._snapshot(c, company, p):
            raise DomainError("FILING_BASIS_CHANGED", "계산 후 원장·기간·근거가 변경되었습니다. 새 계산과 승인이 필요합니다.")
        if calc["blockers"] or not p["closed"] or not self.svc._readiness(c, company, p)["ready"]:
            raise DomainError("FILING_BLOCKED", "마감 또는 검토 준비가 완료되지 않았습니다.")
        return calc, digest({"company": company, "filing": f["id"], "calculation": calc, "label": f["label"], "environment": "sandbox"})

    def create(self, actor, company, data: FilingInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            calc = self.db.get(c, company, "calculation", data.calculation_id)
            rec = self.db.create(c, company, "filing", {"calculation_id": calc["id"], "label": data.label,
                "tax_type": calc["tax_type"], "state": "DRAFT", "author": actor.id, "approved_by": None,
                "approval_hash": None, "approval_expires": 0, "environment": "sandbox", "official_form": False,
                "payable_preview": max(0, calc["totals"]["net_vat"]), "payment_status": "UNCONFIRMED",
                "notice": "내부 검토·모의 제출용. 실제 신고/납부가 아닙니다."})
            self.db.audit(c, company, actor.id, "filing.drafted", rec["id"])
            return rec

    def approve(self, actor, company, ident, expected, reason):
        if not reason.strip(): raise DomainError("REASON_REQUIRED", "신고 검토 사유를 기록하세요.", 422)
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS)
            f = self.db.get(c, company, "filing", ident)
            if f["state"] != "DRAFT": raise DomainError("INVALID_STATE", "초안만 검토 승인할 수 있습니다.")
            if f["author"] == actor.id: raise DomainError("SELF_APPROVAL", "작성자가 자신의 신고안을 독립 승인할 수 없습니다.", 403)
            _, basis = self._basis(c, company, f)
            rec = self.db.replace(c, company, "filing", ident, {**f, "state": "APPROVED", "approved_by": actor.id, "approval_hash": basis, "approval_expires": int(time.time())+28800, "review_note": reason}, expected)
            self.db.audit(c, company, actor.id, "filing.sandbox_approved", ident, {"basis": basis})
            return rec

    def submit(self, actor, company, ident, data: SubmitInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS)
            if data.environment != "sandbox":
                raise DomainError("LIVE_CHANNEL_UNCONFIGURED", "실제 신고 채널·인증·서식 검증이 연결되지 않았습니다. 외부 제출은 실행하지 않습니다.")
            f = self.db.get(c, company, "filing", ident)
            if f["state"] in ("SUBMISSION_UNKNOWN", "SUBMITTING"):
                raise DomainError("RECONCILE_REQUIRED", "제출 결과를 먼저 조회하세요. 다시 전송하지 않습니다.")
            if f["state"] in ("SIMULATED_RECEIVED", "SIMULATED_REJECTED"):
                if f.get("submit_request_revision") != data.expected_revision:
                    raise DomainError("STALE_VERSION", "신고 버전이 다릅니다.")
                return {"filing": f, "replayed": True, "external_requests": 0}
            if f["state"] != "APPROVED": raise DomainError("APPROVAL_REQUIRED", "독립 검토 승인이 필요합니다.")
            self.auth.guard(c, Actor(f.get("approved_by", ""), "approval-reviewer"), company, REVIEWERS)
            _, basis = self._basis(c, company, f)
            if basis != f["approval_hash"] or f["approval_expires"] <= time.time():
                raise DomainError("INVALID_APPROVAL", "승인 대상이 변경되었거나 만료되었습니다.")
            if f["revision"] != data.expected_revision: raise DomainError("STALE_VERSION", "신고 버전이 다릅니다.")
            key = digest({"company": company, "filing": ident, "basis": basis, "mode": "sandbox"})
            attempt = self.db.create(c, company, "submission", {"filing_id": ident, "idempotency_key": key,
                "scenario": data.scenario, "state": "PENDING", "environment": "sandbox", "external_requests": 0, "author": actor.id})
            f = self.db.replace(c, company, "filing", ident, {**f, "state": "SUBMITTING", "submission_id": attempt["id"], "submit_request_revision": data.expected_revision}, data.expected_revision)
            self.db.audit(c, company, actor.id, "filing.sandbox_intent", ident, {"idempotency_key": key})
        with self.db.transaction() as c:
            receipt = {"receipt_no": "SIM-" + key[:20].upper(), "environment": "sandbox", "is_real_filing": False,
                "accepted": data.scenario != "reject", "transport": "in_process_simulator"}
            self.db.create(c, company, "simulator_receipt", {"submission_id": attempt["id"], "receipt": receipt}, ident=attempt["id"])
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS)
            f = self.db.get(c, company, "filing", ident)
            attempt = self.db.get(c, company, "submission", attempt["id"])
            if f["state"] in ("SIMULATED_RECEIVED", "SIMULATED_REJECTED"):
                return {"filing": f, "replayed": True, "external_requests": 0}
            state = "SUBMISSION_UNKNOWN" if data.scenario == "timeout_after_accept" else ("SIMULATED_RECEIVED" if receipt["accepted"] else "SIMULATED_REJECTED")
            shown_receipt = None if state == "SUBMISSION_UNKNOWN" else receipt
            f = self.db.replace(c, company, "filing", ident, {**f, "state": state, "receipt": shown_receipt}, f["revision"])
            self.db.replace(c, company, "submission", attempt["id"], {**attempt, "state": state, "receipt": shown_receipt}, attempt["revision"])
            self.db.audit(c, company, actor.id, "filing.sandbox_result", ident, {"state": state})
            return {"filing": f, "replayed": False, "external_requests": 0}

    def reconcile(self, actor, company, ident):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS)
            f = self.db.get(c, company, "filing", ident)
            if f["state"] not in ("SUBMISSION_UNKNOWN", "SUBMITTING"):
                return {"filing": f, "queried": False, "external_requests": 0}
            row = c.execute("SELECT * FROM objects WHERE company_id=? AND kind='simulator_receipt' AND id=?", (company, f["submission_id"])).fetchone()
            if not row:
                return {"filing": f, "queried": True, "result": "STILL_UNKNOWN_DO_NOT_RESEND", "external_requests": 0}
            receipt = self.db.unpack(row)["receipt"]
            state = "SIMULATED_RECEIVED" if receipt["accepted"] else "SIMULATED_REJECTED"
            f = self.db.replace(c, company, "filing", ident, {**f, "state": state, "receipt": receipt}, f["revision"])
            attempt = self.db.get(c, company, "submission", f["submission_id"])
            self.db.replace(c, company, "submission", attempt["id"], {**attempt, "state": state, "receipt": receipt}, attempt["revision"])
            self.db.audit(c, company, actor.id, "filing.sandbox_reconciled", ident, {"state": state})
            return {"filing": f, "queried": True, "external_requests": 0}

    def record_payment(self, actor, company, ident, data: PaymentInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            f = self.db.get(c, company, "filing", ident)
            if f["state"] != "SIMULATED_RECEIVED":
                raise DomainError("NOT_RECEIVED", "모의 접수 결과가 먼저 필요합니다.")
            self.svc._evidence_check(c, company, [data.evidence_id], required=True)
            payments = [p for p in self.db.list(c, company, "payment") if p["filing_id"] == ident]
            if any(p["evidence_id"] == data.evidence_id for p in payments):
                raise DomainError("PAYMENT_EVIDENCE_DUPLICATE", "같은 증빙으로 이미 납부 기록이 있습니다.")
            if sum(p["amount"] for p in payments)+data.amount > f["payable_preview"]:
                raise DomainError("PAYMENT_OVER_AMOUNT", "기록액이 검토용 납부 예상액을 초과합니다.")
            rec = self.db.create(c, company, "payment", {**data.model_dump(), "filing_id": ident, "author": actor.id, "status": "MANUAL_RECORDED", "bank_verified": False, "environment": "sandbox"})
            self.db.audit(c, company, actor.id, "payment.manual_recorded", rec["id"], {"amount": data.amount, "bank_verified": False})
            return rec
