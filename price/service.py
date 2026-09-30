"""Company-scoped accounting workflows. All state changes are transactional."""
from __future__ import annotations
import csv
import io
import json
import re
import time
from datetime import date
from .auth import Actor, Auth
from .db import Database, DomainError, canonical, digest, now, payload, uid
from .models import (TransactionInput, FactsInput, JournalInput, PeriodInput, CoverageInput,
                     AllocationInput, AuthorityInput, CalculationInput, UserInput, ConsultInput)
from .evidence import decode_evidence
from .calculations import validate_lines
from .ai import Advisor

WRITERS = ("owner", "preparer", "reviewer")
REVIEWERS = ("owner", "reviewer")
PUBLIC_KINDS = {"transaction", "case", "journal", "period", "allocation", "authority", "calculation", "filing", "payment", "consultation", "agent_run", "submission"}


class Service:
    def __init__(self, db: Database, advisor: Advisor | None = None):
        self.db, self.auth, self.advisor = db, Auth(db), advisor or Advisor()

    def list_objects(self, actor, company, kind):
        if kind not in PUBLIC_KINDS:
            raise DomainError("NOT_FOUND", "지원하지 않는 목록입니다.", 404)
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            return self.db.list(c, company, kind)

    def get_object(self, actor, company, kind, ident):
        if kind not in PUBLIC_KINDS:
            raise DomainError("NOT_FOUND", "지원하지 않는 대상입니다.", 404)
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            return self.db.get(c, company, kind, ident)

    def object_history(self, actor, company, kind, ident):
        if kind not in PUBLIC_KINDS:
            raise DomainError("NOT_FOUND", "대상을 찾을 수 없습니다.", 404)
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            current = self.db.get(c, company, kind, ident)
            old = [self.db.unpack(r) for r in c.execute("SELECT * FROM object_history WHERE company_id=? AND kind=? AND id=? ORDER BY revision", (company, kind, ident))]
            return {"revisions": old + [current], "current_revision": current["revision"]}

    def add_user(self, actor, company, data: UserInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, ("owner",))
            rec = self.auth.add_user(c, company, data)
            self.db.audit(c, company, actor.id, "member.created", rec["id"], {"role": rec["role"]})
            return rec

    def overview(self, actor, company):
        with self.db.connect() as c:
            role = self.auth.guard(c, actor, company)
            profile = json.loads(c.execute("SELECT body FROM companies WHERE id=?", (company,)).fetchone()[0])
            txs = self.db.list(c, company, "transaction")
            journals = self.db.list(c, company, "journal")
            cases = self.db.list(c, company, "case")
            periods = self.db.list(c, company, "period")
            return {"company": {**profile, "id": company}, "role": role,
                    "counts": {"transactions": len(txs), "evidence": c.execute("SELECT count(*) FROM blobs WHERE company_id=?", (company,)).fetchone()[0],
                               "posted": sum(j["state"] == "POSTED" for j in journals),
                               "pending_journals": sum(j["state"] not in ("POSTED", "SUPERSEDED") for j in journals),
                               "open_cases": sum(x["state"] not in ("CLOSED", "SUPERSEDED") for x in cases)},
                    "cases": [x for x in cases if x["state"] not in ("CLOSED", "SUPERSEDED")][-8:],
                    "periods": [{**p, "readiness": self._readiness(c, company, p)} for p in periods],
                    "llm": {"configured": self.advisor.configured, "live_verified": False},
                    "live_filing": False, "ledger_mode": "validation", "release": "0.2.1-ubuntu"}

    def upload_evidence(self, actor, company, filename, content_base64):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company, WRITERS)
        data = decode_evidence(filename, content_base64)
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            old = c.execute("SELECT id,sha256,filename,mime,length(content) AS size,extraction_status,created_at FROM blobs WHERE company_id=? AND sha256=?", (company, data["sha256"])).fetchone()
            if old:
                return {**dict(old), "deduplicated": True}
            ident = uid("evidence")
            c.execute("INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?,?)", (company, ident, data["sha256"], data["filename"], data["mime"], data["content"], data["text_content"], data["extraction_status"], now()))
            self.db.audit(c, company, actor.id, "evidence.stored", ident, {"sha256": data["sha256"], "size": len(data["content"]), "extraction": data["extraction_status"]})
            return {"id": ident, "filename": filename, "sha256": data["sha256"], "mime": data["mime"], "size": len(data["content"]), "extraction_status": data["extraction_status"], "deduplicated": False}

    def evidence_list(self, actor, company):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            return [dict(r) for r in c.execute("SELECT id,sha256,filename,mime,length(content) AS size,extraction_status,created_at FROM blobs WHERE company_id=? ORDER BY created_at", (company,))]

    def evidence_content(self, actor, company, ident):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company)
            row = c.execute("SELECT * FROM blobs WHERE company_id=? AND id=?", (company, ident)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "증빙에 접근할 수 없습니다.", 404)
            import hashlib
            if hashlib.sha256(row["content"]).hexdigest() != row["sha256"]:
                raise DomainError("EVIDENCE_INTEGRITY", "원본 해시가 맞지 않습니다.", 500)
            self.db.audit(c, company, actor.id, "evidence.downloaded", ident)
            return dict(row)

    def _evidence_check(self, c, company, ids, required=False):
        if required and not ids:
            raise DomainError("MISSING_EVIDENCE", "원본 증빙이 필요합니다.")
        for ident in ids:
            if not c.execute("SELECT 1 FROM blobs WHERE company_id=? AND id=?", (company, ident)).fetchone():
                raise DomainError("EVIDENCE_NOT_FOUND", "현재 회사의 증빙이 아닙니다.", 404)

    def _locked(self, c, company, event_date):
        opening=c.execute("SELECT json_extract(body,'$.event_date') FROM objects WHERE company_id=? AND kind='journal' AND json_extract(body,'$.state')='POSTED' AND json_extract(body,'$.opening_batch_id') IS NOT NULL LIMIT 1",(company,)).fetchone()
        if opening and event_date<opening[0]:raise DomainError('BEFORE_OPENING_DATE','기초잔액 이전 거래는 이관 범위 재검토가 필요합니다.')
        if any(p["closed"] and p["start"] <= event_date <= p["end"] for p in self.db.list(c, company, "period")):
            raise DomainError("PERIOD_LOCKED", "잠긴 기간은 변경할 수 없습니다.")

    def _new_tx(self, c, actor, company, data):
        values = data.model_dump(mode="json")
        self._evidence_check(c, company, values["evidence_ids"])
        namespace, h = "transaction:" + values["source"], digest(values)
        old = c.execute("SELECT * FROM external_ids WHERE company_id=? AND namespace=? AND external_id=?", (company, namespace, values["external_id"])).fetchone()
        if old:
            if old["body_hash"] != h:
                raise DomainError("EXTERNAL_ID_CONFLICT", "같은 외부 ID에 다른 내용이 있습니다. 수정 거래를 별도 확인하세요.")
            return {**self.db.get(c, company, "transaction", old["object_id"]), "deduplicated": True}
        self._locked(c, company, values["event_date"])
        rec = self.db.create(c, company, "transaction", {**values, "source_hash": h, "author": actor.id})
        c.execute("INSERT INTO external_ids VALUES(?,?,?,?,?)", (company, namespace, values["external_id"], h, rec["id"]))
        self.db.audit(c, company, actor.id, "transaction.created", rec["id"], {"source_hash": h})
        return {**rec, "deduplicated": False}

    def create_transaction(self, actor, company, data: TransactionInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            return self._new_tx(c, actor, company, data)

    def import_csv(self, actor, company, text):
        from pydantic import ValidationError
        with self.db.connect() as c:
            self.auth.guard(c, actor, company, WRITERS)
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise DomainError("CSV_HEADER", "CSV 헤더가 없거나 중복되었습니다.", 422)
        parsed, errors = [], []
        for n, row in enumerate(reader, 2):
            if n > 2001:
                raise DomainError("CSV_TOO_MANY_ROWS", "한 번에 2,000행 이하로 가져오세요.", 413)
            if None in row:
                errors.append({"row": n, "error": "헤더보다 열이 많습니다."}); continue
            row = {k: v.strip() for k, v in row.items() if v is not None and v.strip() != ""}
            try:
                for key in ("net", "tax", "gross"):
                    if key in row:
                        if not re.fullmatch(r"[0-9]+", row[key]):
                            raise ValueError(key + ": 금액은 음수가 아닌 정수로 입력하세요.")
                        row[key] = int(row[key])
                for key in ("deductible", "related_party"):
                    if key in row:
                        if row[key].lower() not in ("true", "false"):
                            raise ValueError(key + ": true/false만 가능합니다.")
                        row[key] = row[key].lower() == "true"
                if "evidence_ids" in row:
                    row["evidence_ids"] = json.loads(row["evidence_ids"])
                parsed.append(TransactionInput.model_validate(row))
            except (ValueError, TypeError, ValidationError) as exc:
                errors.append({"row": n, "error": str(exc)[:700]})
        if errors or not parsed:
            raise DomainError("CSV_INVALID", "CSV 입력을 취소했습니다. 오류 행을 수정하세요.", 422, errors or [{"error": "거래 행이 없습니다."}])
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            results = [self._new_tx(c, actor, company, x) for x in parsed]
            self.db.audit(c, company, actor.id, "csv.imported", "batch", {"rows": len(results)})
            return {"rows": results, "created": sum(not r["deduplicated"] for r in results), "deduplicated": sum(r["deduplicated"] for r in results)}

    def _supersede(self, c, company, tx_id):
        for kind in ("case", "journal"):
            for record in self.db.list(c, company, kind):
                if record.get("transaction_id") == tx_id and record.get("state") not in ("POSTED", "CLOSED", "SUPERSEDED"):
                    self.db.replace(c, company, kind, record["id"], {**record, "state": "SUPERSEDED"}, record["revision"])

    def update_facts(self, actor, company, ident, data: FactsInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            rec = self.db.get(c, company, "transaction", ident)
            self._locked(c, company, rec["event_date"])
            if any(j.get("transaction_id") == ident and j["state"] == "POSTED" for j in self.db.list(c, company, "journal")):
                raise DomainError("POSTED_FACTS_IMMUTABLE", "확정 거래는 덮어쓸 수 없습니다. 정정 사건과 역분개로 처리하세요.")
            values = payload(rec)
            values.update(data.model_dump(exclude={"expected_revision", "reason"}))
            TransactionInput.model_validate({k: values[k] for k in TransactionInput.model_fields})
            self._evidence_check(c, company, values["evidence_ids"])
            changed = self.db.replace(c, company, "transaction", ident, values, data.expected_revision)
            self._supersede(c, company, ident)
            self.db.audit(c, company, actor.id, "transaction.facts_updated", ident, {"revision": changed["revision"], "reason": data.reason})
            return changed

    def _new_journal(self, c, actor, company, data: dict, *, transaction=None, reversal_of=None):
        self._locked(c, company, data["event_date"])
        self._evidence_check(c, company, data["evidence_ids"], required=True)
        validate_lines(data["lines"])
        if transaction:
            for j in self.db.list(c, company, "journal"):
                if j.get("transaction_id") == transaction["id"] and j["state"] != "SUPERSEDED":
                    raise DomainError("JOURNAL_EXISTS", "이 거래의 전표가 이미 있습니다.")
        record = {**data, "transaction_id": transaction["id"] if transaction else None,
                  "transaction_revision": transaction["revision"] if transaction else None,
                  "author": actor.id, "state": "DRAFT", "reversal_of": reversal_of,
                  "approved_by": None, "approval_hash": None, "approval_expires": 0}
        rec = self.db.create(c, company, "journal", record)
        self.db.audit(c, company, actor.id, "journal.drafted", rec["id"], {"transaction": record["transaction_id"], "reversal_of": reversal_of})
        return rec

    def create_journal(self, actor, company, data: JournalInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            values = data.model_dump(mode="json")
            tx = self.db.get(c, company, "transaction", data.transaction_id) if data.transaction_id else None
            if tx and (tx["event_date"] != values["event_date"] or tx["currency"] != "KRW"):
                raise DomainError("JOURNAL_SOURCE_MISMATCH", "거래의 날짜·통화와 전표가 맞지 않습니다.")
            return self._new_journal(c, actor, company, values, transaction=tx)

    @staticmethod
    def journal_hash(j):
        return digest({k: j.get(k) for k in ("transaction_id", "transaction_revision", "event_date", "description", "lines", "evidence_ids", "author", "reversal_of")})

    def _journal_valid(self, c, company, j):
        self._locked(c, company, j["event_date"])
        self._evidence_check(c, company, j["evidence_ids"], required=True)
        validate_lines(j["lines"])
        if j.get("transaction_id"):
            tx = self.db.get(c, company, "transaction", j["transaction_id"])
            if tx["revision"] != j["transaction_revision"]:
                raise DomainError("SOURCE_CHANGED", "원거래가 변경되어 재검토가 필요합니다.")
            if tx["currency"] != "KRW" or tx["related_party"] or tx["use_type"] in ("asset", "other"):
                raise DomainError("UNSUPPORTED_SOURCE", "이 거래는 전문 검토/지원 모듈이 필요합니다.")
            if sum(l["amount"] for l in j["lines"] if l["side"] == "debit") != tx["gross"]:
                raise DomainError("SOURCE_AMOUNT_MISMATCH", "전표 합계가 원거래 총액과 다릅니다.")
            assessment = self._assessment(c, company, tx)
            if assessment["questions"] or assessment["blockers"]:
                raise DomainError("SOURCE_FACTS_UNRESOLVED", "원거래의 필수 사실/검토가 해결되지 않았습니다.")
            normalize = lambda lines: sorted((x["account"], x["side"], x["amount"]) for x in lines)
            if normalize(j["lines"]) != normalize(assessment["lines"]):
                raise DomainError("UNSUPPORTED_ACCOUNTING_OVERRIDE", "지원 거래의 검증 분개와 다릅니다. 임의 변경을 확정하지 않습니다.")
        elif j.get('opening_batch_id'):
            from .opening_books import validate_opening_entry
            validate_opening_entry(self,c,company,j)
        elif not j.get("reversal_of") and any(x["account"] in ("vat_input", "vat_output") for x in j["lines"]):
            raise DomainError("TAX_SOURCE_REQUIRED", "세액 계정에는 검증된 원거래 또는 역분개가 필요합니다.")
        if j.get("reversal_of"):
            original = self.db.get(c, company, "journal", j["reversal_of"])
            expected = [{**x, "side": "credit" if x["side"] == "debit" else "debit"} for x in original["lines"]]
            if original["state"] != "POSTED" or j["lines"] != expected:
                raise DomainError("INVALID_REVERSAL", "원 확정전표의 정확한 반대 전표여야 합니다.")

    def _journal_contributors(self, c, company, journal):
        # Preserve human origin even when a worker generated the proposal.
        contributors = {journal["author"]}
        if journal.get("transaction_id"):
            tx = self.db.get(c, company, "transaction", journal["transaction_id"])
            contributors.add(tx["author"])
            contributors.update(row[0] for row in c.execute(
                "SELECT DISTINCT actor FROM audit WHERE company_id=? AND entity_id=? AND action='transaction.facts_updated'",
                (company, tx["id"])))
        return contributors

    def transition_journal(self, actor, company, ident, action, expected, reason=""):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS if action == "approve" else WRITERS)
            j = self.db.get(c, company, "journal", ident)
            transitions = {"validate": ("DRAFT", "VALIDATED"), "approve": ("VALIDATED", "APPROVED"), "post": ("APPROVED", "POSTED")}
            if action not in transitions or j["state"] != transitions[action][0]:
                raise DomainError("INVALID_STATE", "전표 처리 순서가 올바르지 않습니다.")
            self._journal_valid(c, company, j)
            if action == "approve":
                if actor.id in self._journal_contributors(c, company, j):
                    raise DomainError("SELF_APPROVAL", "원거래 작성·사실 수정·전표 생성에 참여하지 않은 검토자가 승인해야 합니다.", 403)
                j.update(approved_by=actor.id, approval_hash=self.journal_hash(j), approval_expires=int(time.time()) + 28800)
            if action == "post":
                self.auth.guard(c, Actor(j.get("approved_by", ""), "approval-reviewer"), company, REVIEWERS)
                if j.get("approved_by") in self._journal_contributors(c, company, j):
                    raise DomainError("SELF_APPROVAL", "독립 승인 조건을 충족하지 않아 확정을 차단했습니다.", 403)
                if not j.get("approved_by") or j["approval_hash"] != self.journal_hash(j) or j["approval_expires"] <= time.time():
                    raise DomainError("INVALID_APPROVAL", "승인이 없거나 변경·만료되었습니다.")
                for other in self.db.list(c, company, "journal"):
                    if other["id"] != ident and other["state"] == "POSTED" and j.get("transaction_id") and other.get("transaction_id") == j["transaction_id"]:
                        raise DomainError("DUPLICATE_POST", "같은 거래가 이미 확정되었습니다.")
            j["state"] = transitions[action][1]
            updated = self.db.replace(c, company, "journal", ident, j, expected)
            self.db.audit(c, company, actor.id, "journal." + action, ident, {"hash": self.journal_hash(j), "reason": reason})
            if action == "post":
                for case in self.db.list(c, company, "case"):
                    if case.get("journal_id") == ident and case["state"] == "PROPOSED":
                        self.db.replace(c, company, "case", case["id"], {**case, "state": "CLOSED", "resolution": "검토 승인 후 전표 확정"}, case["revision"])
            return updated

    def reverse_journal(self, actor, company, ident, event_date, reason, expected):
        if not reason.strip():
            raise DomainError("REASON_REQUIRED", "역분개 사유가 필요합니다.", 422)
        event_date = date.fromisoformat(event_date).isoformat()
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            j = self.db.get(c, company, "journal", ident)
            if j["revision"] != expected:
                raise DomainError("STALE_VERSION", "전표 버전이 변경되었습니다.")
            if j["state"] != "POSTED":
                raise DomainError("INVALID_STATE", "확정전표만 역분개할 수 있습니다.")
            if any(x.get("reversal_of") == ident and x["state"] != "SUPERSEDED" for x in self.db.list(c, company, "journal")):
                raise DomainError("REVERSAL_EXISTS", "이미 역분개가 있습니다.")
            return self._new_journal(c, actor, company, {"event_date": event_date, "description": "역분개: " + reason,
                "evidence_ids": j["evidence_ids"], "lines": [{**x, "side": "credit" if x["side"] == "debit" else "debit"} for x in j["lines"]],
                **({'current_period_activity':False} if j.get('current_period_activity') is False else {})}, reversal_of=ident)

    def _assessment(self, c, company, tx):
        questions, blockers, lines = [], [], []
        if tx["currency"] != "KRW": blockers.append("외화 환산 모듈 미검증")
        if tx["related_party"]: blockers.append("대표자·관련자 거래 전문 검토 필요")
        if tx["use_type"] in ("asset", "other"): blockers.append("자산/비정형 거래 자동기장 범위 밖")
        if not tx["business_purpose"]: questions.append("이 거래의 업무 목적을 확인해 주세요.")
        if not tx["evidence_ids"]: questions.append("원본 증빙을 연결해 주세요.")
        if tx["kind"] == "invoice":
            if tx["tax_category"] == "unknown": questions.append("과세·면세·영세 구분을 확인해 주세요.")
            if tx["direction"] == "purchase" and tx["tax_category"] == "standard" and tx["deductible"] is None:
                questions.append("이 매입세액의 공제 가능 여부와 근거를 확인해 주세요.")
        else:
            allocated = sum(a["amount"] for a in self.db.list(c, company, "allocation") if a["bank_id"] == tx["id"])
            if allocated != tx["gross"]: questions.append("은행 거래를 청구 거래와 전액 대사해 주세요.")
        if blockers or questions:
            return {"state": "BLOCKED_SCOPE" if blockers else "NEEDS_FACTS", "questions": questions, "blockers": blockers, "risk": "high" if blockers else "unknown", "lines": []}
        def line(account, side, amount):
            if amount: lines.append({"account": account, "side": side, "amount": amount})
        if tx["direction"] == "sale":
            line("receivable", "debit", tx["gross"]); line("revenue", "credit", tx["net"]); line("vat_output", "credit", tx["tax"])
        elif tx["direction"] == "purchase":
            deductible = tx["deductible"] is True and tx["tax_category"] == "standard"
            line("inventory" if tx["use_type"] == "resale" else "expense", "debit", tx["net"] if deductible else tx["gross"])
            if deductible: line("vat_input", "debit", tx["tax"])
            line("payable", "credit", tx["gross"])
        elif tx["direction"] == "receipt":
            line("cash", "debit", tx["gross"]); line("receivable", "credit", tx["gross"])
        else:
            line("payable", "debit", tx["gross"]); line("cash", "credit", tx["gross"])
        return {"state": "PROPOSED", "questions": [], "blockers": [], "risk": "review_required", "lines": lines}

    def analyze(self, actor, company, ident, expected_revision=None):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, ("worker",) if actor.worker else WRITERS)
            tx = self.db.get(c, company, "transaction", ident)
            if expected_revision is not None and tx["revision"] != expected_revision:
                raise DomainError("STALE_JOB", "작업 등록 후 거래가 변경되었습니다.")
            for case in self.db.list(c, company, "case"):
                if case.get("transaction_id") == ident and case.get("transaction_revision") == tx["revision"] and case["state"] != "SUPERSEDED":
                    return {"case": case, "journal": self.db.get(c, company, "journal", case["journal_id"]) if case.get("journal_id") else None, "reused": True}
            result = self._assessment(c, company, tx)
            journal = None
            if result["lines"]:
                journal = self._new_journal(c, actor, company, {"event_date": tx["event_date"], "description": tx["business_purpose"], "lines": result["lines"], "evidence_ids": tx["evidence_ids"]}, transaction=tx)
                self._journal_valid(c, company, journal)
                journal = self.db.replace(c, company, "journal", journal["id"], {**journal, "state": "VALIDATED"}, journal["revision"])
            case = self.db.create(c, company, "case", {"transaction_id": ident, "transaction_revision": tx["revision"], "event_date": tx["event_date"], "title": tx["counterparty"] + " · " + tx["external_id"], **{k: v for k, v in result.items() if k != "lines"}, "journal_id": journal["id"] if journal else None, "execution_mode": "deterministic_rules_not_llm", "author": actor.id})
            self.db.create(c, company, "agent_run", {"transaction_id": ident, "case_id": case["id"], "mode": "deterministic", "stages": ["case_manager", "evidence_bookkeeper", "scope_verifier", "proposal_validator"], "result": result["state"], "live_model_called": False})
            self.db.audit(c, company, actor.id, "transaction.analyzed", ident, {"case": case["id"], "result": result["state"]})
            return {"case": case, "journal": journal, "reused": False}

    def allocate(self, actor, company, data: AllocationInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            bank = self.db.get(c, company, "transaction", data.bank_id)
            invoice = self.db.get(c, company, "transaction", data.invoice_id)
            self._locked(c, company, bank["event_date"])
            if bank["kind"] != "bank" or invoice["kind"] != "invoice" or (bank["direction"], invoice["direction"]) not in (("receipt", "sale"), ("payment", "purchase")) or bank["currency"] != invoice["currency"]:
                raise DomainError("ALLOCATION_MISMATCH", "은행·청구 거래의 종류/방향/통화가 맞지 않습니다.")
            allocations = self.db.list(c, company, "allocation")
            bank_total = sum(a["amount"] for a in allocations if a["bank_id"] == bank["id"])
            inv_total = sum(a["amount"] for a in allocations if a["invoice_id"] == invoice["id"])
            if bank_total + data.amount > bank["gross"] or inv_total + data.amount > invoice["gross"]:
                raise DomainError("OVER_ALLOCATION", "배분 금액이 미대사 잔액을 초과합니다.")
            if any(j.get("transaction_id") == bank["id"] and j["state"] == "POSTED" for j in self.db.list(c, company, "journal")):
                raise DomainError("POSTED_BANK_IMMUTABLE", "확정 은행 전표의 배분은 정정 검토가 필요합니다.")
            rec = self.db.create(c, company, "allocation", {**data.model_dump(), "confirmed_by": actor.id})
            self.db.replace(c, company, "transaction", bank["id"], bank, bank["revision"])
            self._supersede(c, company, bank["id"])
            self.db.audit(c, company, actor.id, "reconciliation.allocated", rec["id"], {"amount": data.amount})
            return rec

    def create_period(self, actor, company, data: PeriodInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            for p in self.db.list(c, company, "period"):
                if p["start"] == data.start.isoformat() and p["end"] == data.end.isoformat():
                    raise DomainError("PERIOD_EXISTS", "같은 기간이 이미 있습니다.")
            rec = self.db.create(c, company, "period", {**data.model_dump(mode="json"), "closed": False, "opening_reviewed": False, "sources_confirmed": [], "coverage_note": "", "author": actor.id})
            self.db.audit(c, company, actor.id, "period.created", rec["id"])
            return rec

    def coverage(self, actor, company, ident, data: CoverageInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS)
            p = self.db.get(c, company, "period", ident)
            if p["closed"]: raise DomainError("PERIOD_LOCKED", "마감 기간은 수집확인을 변경할 수 없습니다.")
            if not set(data.sources_confirmed).issubset(p["required_sources"]):
                raise DomainError("UNKNOWN_SOURCE", "기간에 정의한 수집원만 확인할 수 있습니다.")
            rec = self.db.replace(c, company, "period", ident, {**p, "opening_reviewed": data.opening_reviewed, "sources_confirmed": data.sources_confirmed, "coverage_note": data.evidence_note, "confirmed_by": actor.id, "confirmation_type": "manual_attestation"}, data.expected_revision)
            self.db.audit(c, company, actor.id, "period.coverage_attested", ident, {"note": data.evidence_note})
            return rec

    def _readiness(self, c, company, p):
        blockers = []
        from .finance_ops import uploaded_source_blockers
        blockers.extend(uploaded_source_blockers(self.db, c, company, p))
        within = lambda r: p["start"] <= r.get("event_date", "") <= p["end"]
        txs = [t for t in self.db.list(c, company, "transaction") if within(t)]
        journals = [j for j in self.db.list(c, company, "journal") if within(j)]
        cases = [x for x in self.db.list(c, company, "case") if within(x) and x["state"] not in ("CLOSED", "SUPERSEDED")]
        if not p["opening_reviewed"]: blockers.append({"code": "OPENING_UNREVIEWED", "message": "기초잔액·기초자료 확인이 필요합니다."})
        missing = set(p["required_sources"]) - set(p["sources_confirmed"])
        if missing: blockers.append({"code": "SOURCES_UNCONFIRMED", "message": "수집 범위 미확인: " + ", ".join(sorted(missing))})
        extra = {t["source"] for t in txs} - set(p["required_sources"])
        if extra: blockers.append({"code": "UNDECLARED_SOURCES", "message": "기간에 선언하지 않은 수집원: " + ", ".join(sorted(extra))})
        posted_tx = {j.get("transaction_id") for j in journals if j["state"] == "POSTED"}
        for t in txs:
            if t["id"] not in posted_tx: blockers.append({"code": "UNPOSTED_TRANSACTION", "id": t["id"], "message": t["counterparty"] + " 미확정 거래"})
        for j in journals:
            if j["state"] not in ("POSTED", "SUPERSEDED"): blockers.append({"code": "PENDING_JOURNAL", "id": j["id"], "message": "검토/확정 대기 전표"})
        for x in cases: blockers.append({"code": "OPEN_CASE", "id": x["id"], "message": x["title"] + " · " + x["state"]})
        from .book_schedules import due_schedule_blockers
        blockers.extend(due_schedule_blockers(self.db,c,company,p))
        from .inventory_books import inventory_blockers
        blockers.extend(inventory_blockers(self.db,c,company,p))
        if not self.db.verify_audit(c, company)["valid"]: blockers.append({"code": "AUDIT_INVALID", "message": "감사체인 검증 실패"})
        return {"ready": not blockers, "blockers": blockers, "transactions": len(txs), "basis": "manual_source_attestation_and_validation_ledger", "official_tax_ready": False}

    def readiness(self, actor, company, ident):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            return self._readiness(c, company, self.db.get(c, company, "period", ident))

    def period_action(self, actor, company, ident, action, expected, reason):
        if action not in ("close", "reopen") or not reason.strip():
            raise DomainError("REASON_REQUIRED", "마감/재개 사유가 필요합니다.", 422)
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS)
            p = self.db.get(c, company, "period", ident)
            if p["closed"] == (action == "close"):
                raise DomainError("INVALID_STATE", "이미 요청한 기간 상태입니다.")
            if action == "close":
                r = self._readiness(c, company, p)
                if not r["ready"]: raise DomainError("CLOSING_BLOCKED", "미해결 항목 때문에 마감할 수 없습니다.", 409, r["blockers"])
            rec = self.db.replace(c, company, "period", ident, {**p, "closed": action == "close", "last_action_by": actor.id, "last_action_reason": reason}, expected)
            self.db.audit(c, company, actor.id, "period." + action, ident, {"reason": reason})
            return rec

    def trial_balance(self, actor, company):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            totals = {}
            for j in self.db.list(c, company, "journal"):
                if j["state"] != "POSTED": continue
                for x in j["lines"]:
                    r = totals.setdefault(x["account"], {"account": x["account"], "debit": 0, "credit": 0})
                    r[x["side"]] += x["amount"]
            rows = [{**x, "net_debit": x["debit"]-x["credit"]} for x in totals.values()]
            debit, credit = sum(x["debit"] for x in rows), sum(x["credit"] for x in rows)
            return {"rows": rows, "debit": debit, "credit": credit, "balanced": debit == credit, "ledger_mode": "validation"}

    def add_authority(self, actor, company, data: AuthorityInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            rec = self.db.create(c, company, "authority", {**data.model_dump(mode="json"), "author": actor.id, "reviewed_by": None, "reviewed": False, "retrieval": "user_supplied_not_auto_verified"})
            self.db.audit(c, company, actor.id, "authority.created", rec["id"])
            return rec

    def review_authority(self, actor, company, ident, expected, reason):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, REVIEWERS)
            rec = self.db.get(c, company, "authority", ident)
            if rec["author"] == actor.id: raise DomainError("SELF_APPROVAL", "다른 검토자가 근거를 검토해야 합니다.", 403)
            if not reason.strip(): raise DomainError("REASON_REQUIRED", "검토 근거가 필요합니다.", 422)
            rec = self.db.replace(c, company, "authority", ident, {**rec, "reviewed": True, "reviewed_by": actor.id, "review_note": reason}, expected)
            self.db.audit(c, company, actor.id, "authority.reviewed", ident)
            return rec

    def _snapshot(self, c, company, p):
        from .finance_ops import source_snapshot
        within = lambda r: p["start"] <= r.get("event_date", "") <= p["end"]
        return digest({"company": company, "period": p,
                       "transactions": [x for x in self.db.list(c, company, "transaction") if within(x)],
                       "journals": [x for x in self.db.list(c, company, "journal") if within(x) and x["state"] != "SUPERSEDED"],
                       "cases": [x for x in self.db.list(c, company, "case") if within(x) and x["state"] != "SUPERSEDED"],
                       "book_schedules": [x for x in self.db.list(c,company,"book_schedule") if x['input']['classification_date']<=p['end']],
                       "authorities": self.db.list(c, company, "authority"), "source_snapshot": source_snapshot(self.db,c,company,p), "calculation_method": "ledger-vat-aggregation/1"})

    def calculate(self, actor, company, data: CalculationInput):
        if data.tax_type != "VAT":
            raise DomainError("TAX_RULEPACK_NOT_VERIFIED", "법인세·원천세의 한국 공식 규칙·서식은 아직 검증되지 않았습니다. 임의 세액을 반환하지 않습니다.")
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            p = self.db.get(c, company, "period", data.period_id)
            totals = {"sales_net": 0, "output_vat": 0, "input_vat": 0}
            trace = []
            for j in self.db.list(c, company, "journal"):
                if j["state"] != "POSTED" or not (p["start"] <= j["event_date"] <= p["end"]): continue
                if j.get('current_period_activity') is False:continue
                for l in j["lines"]:
                    key = {"revenue": "sales_net", "vat_output": "output_vat", "vat_input": "input_vat"}.get(l["account"])
                    if key:
                        signed = l["amount"] * (1 if l["side"] == ("debit" if key == "input_vat" else "credit") else -1)
                        totals[key] += signed
                        trace.append({"journal_id": j["id"], "transaction_id": j.get("transaction_id"), "account": l["account"], "amount": signed, "evidence_ids": j["evidence_ids"]})
            totals["net_vat"] = totals["output_vat"]-totals["input_vat"]
            readiness = self._readiness(c, company, p)
            blockers = readiness["blockers"] + ([] if p["closed"] else [{"code": "PERIOD_NOT_CLOSED", "message": "기간 마감 전 계산입니다."}])
            rec = self.db.create(c, company, "calculation", {"tax_type": "VAT", "period_id": p["id"], "totals": totals, "trace": trace, "dependency_hash": self._snapshot(c, company, p), "blockers": blockers, "author": actor.id,
                "method": "ledger-vat-aggregation/1", "review_only": True, "official_form": False,
                "limitations": ["확정 원장에 입력·검토한 VAT 계정 집계", "전체 세액공제·특례·가산세·전자신고 서식 미포함", "국세청 신고용 세액 검증 아님"]})
            self.db.audit(c, company, actor.id, "tax.preview_calculated", rec["id"], {"snapshot": rec["dependency_hash"]})
            return rec

    def consult(self, actor, company, data: ConsultInput):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company, WRITERS)
            authorities = [self.db.get(c, company, "authority", ident) for ident in data.authority_ids]
            as_of = data.as_of.isoformat()
            if any(not a["reviewed"] or not (a["effective_from"] <= as_of <= a["effective_to"]) for a in authorities):
                raise DomainError("AUTHORITY_NOT_APPLICABLE", "미검토 또는 해당 날짜에 유효하지 않은 근거가 있습니다.")
            transactions = self.db.list(c, company, "transaction")
            cases = self.db.list(c, company, "case")
            facts = {"as_of": as_of, "transaction_count": len(transactions), "open_cases": len([x for x in cases if x["state"] not in ("CLOSED", "SUPERSEDED")]), "ledger_mode": "validation", "unsupported_count": sum(t["currency"] != "KRW" or t["related_party"] for t in transactions)}
            basis_hash = digest({"facts": facts, "authorities": authorities})
        result = self.advisor.consult(data.question, facts, authorities)
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            rec = self.db.create(c, company, "consultation", {"question": data.question, **result, "basis_hash": basis_hash, "author": actor.id})
            self.db.audit(c, company, actor.id, "consultation.recorded", rec["id"], {"provider_status": result["provider_status"]})
            return rec

    def audit_report(self, actor, company):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            rows = [dict(x) for x in c.execute("SELECT * FROM audit WHERE company_id=? ORDER BY seq DESC LIMIT 200", (company,))]
            return {"integrity": self.db.verify_audit(c, company), "events": rows, "limit": 200}
