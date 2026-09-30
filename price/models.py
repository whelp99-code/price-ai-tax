"""Only explicit user-editable fields are accepted; workflow fields are server-owned."""
from __future__ import annotations
from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator
from .accounts import AccountCode

Money = Annotated[StrictInt, Field(ge=0, le=1_000_000_000_000)]
PositiveMoney = Annotated[StrictInt, Field(gt=0, le=1_000_000_000_000)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CompanyInput(Input):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    industry: str = "IT/용역"
    fiscal_month: Annotated[StrictInt, Field(ge=1, le=12)] = 12
    currency: Literal["KRW"] = "KRW"


class LoginInput(Input):
    username: Annotated[str, Field(min_length=1, max_length=80)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UserInput(Input):
    username: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.@-]{3,80}$")]
    password: Annotated[str, Field(min_length=12, max_length=256)]
    role: Literal["owner", "preparer", "reviewer", "viewer"]


class TransactionInput(Input):
    external_id: Annotated[str, Field(min_length=1, max_length=160)]
    source: Annotated[str, Field(min_length=1, max_length=80)] = "manual"
    kind: Literal["invoice", "bank"] = "invoice"
    direction: Literal["sale", "purchase", "receipt", "payment"]
    event_date: date
    counterparty: Annotated[str, Field(min_length=1, max_length=160)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "KRW"
    net: Money
    tax: Money = 0
    gross: PositiveMoney
    tax_category: Literal["standard", "exempt", "zero", "unknown"] = "unknown"
    deductible: StrictBool | None = None
    business_purpose: Annotated[str, Field(max_length=2000)] = ""
    related_party: StrictBool = False
    use_type: Literal["expense", "resale", "asset", "other"] = "expense"
    evidence_ids: Annotated[list[str], Field(max_length=30)] = []
    project: Annotated[str, Field(max_length=160)] = ""
    contract: Annotated[str, Field(max_length=160)] = ""

    @model_validator(mode="after")
    def consistent(self):
        if self.net + self.tax != self.gross:
            raise ValueError("공급가액 + 세액은 총액과 같아야 합니다.")
        if (self.kind == "invoice") != (self.direction in ("sale", "purchase")):
            raise ValueError("거래 종류와 방향이 맞지 않습니다.")
        if self.kind == "bank" and self.tax:
            raise ValueError("은행 거래에 매입/매출 세액을 중복 기록하지 않습니다.")
        if self.tax_category in ("exempt", "zero") and self.tax:
            raise ValueError("면세·영세 분류에는 세액을 입력할 수 없습니다.")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("증빙 ID가 중복되었습니다.")
        return self


class FactsInput(Input):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    business_purpose: Annotated[str, Field(min_length=1, max_length=2000)]
    tax_category: Literal["standard", "exempt", "zero", "unknown"]
    deductible: StrictBool | None = None
    evidence_ids: Annotated[list[str], Field(max_length=30)]
    reason: Text


class RevisionInput(Input):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    reason: Annotated[str, Field(max_length=2000)] = ""


class Line(Input):
    account: AccountCode
    side: Literal["debit", "credit"]
    amount: PositiveMoney


class JournalInput(Input):
    transaction_id: str | None = None
    event_date: date
    description: Text
    lines: Annotated[list[Line], Field(min_length=2, max_length=100)]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=30)]


class EvidenceInput(Input):
    filename: Annotated[str, Field(min_length=1, max_length=160)]
    content_base64: Annotated[str, Field(min_length=1, max_length=3_000_000)]


class PeriodInput(Input):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    start: date
    end: date
    required_sources: Annotated[list[str], Field(min_length=1, max_length=30)] = ["manual"]

    @model_validator(mode="after")
    def order(self):
        if self.start > self.end:
            raise ValueError("기간 시작이 종료보다 늦습니다.")
        if (self.end - self.start).days > 366:
            raise ValueError("최대 1개 사업연도 단위로 생성하세요.")
        if len(set(self.required_sources)) != len(self.required_sources) or any(not s.strip() for s in self.required_sources):
            raise ValueError("수집원은 비어 있거나 중복될 수 없습니다.")
        return self


class CoverageInput(Input):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    opening_reviewed: StrictBool
    sources_confirmed: Annotated[list[str], Field(max_length=30)]
    evidence_note: Text


class AllocationInput(Input):
    bank_id: str
    invoice_id: str
    amount: PositiveMoney
    reason: Text


class AuthorityInput(Input):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    url: Annotated[str, Field(max_length=1200)]
    effective_from: date
    effective_to: date
    excerpt: Annotated[str, Field(min_length=1, max_length=10000)]
    kind: Literal["law", "interpretation", "company_policy", "sample"] = "law"

    @model_validator(mode="after")
    def dates(self):
        if self.effective_from > self.effective_to:
            raise ValueError("효력기간이 잘못되었습니다.")
        from urllib.parse import urlparse
        u = urlparse(self.url)
        if u.scheme != "https" or u.hostname not in {"law.go.kr", "www.law.go.kr", "nts.go.kr", "www.nts.go.kr", "taxlaw.nts.go.kr"} or u.username or u.password:
            raise ValueError("공식 세무·법령 출처의 HTTPS 주소만 등록하세요. 자동 방문하지는 않습니다.")
        return self


class CalculationInput(Input):
    period_id: str
    tax_type: Literal["VAT", "CORPORATE", "WITHHOLDING"] = "VAT"


class FilingInput(Input):
    calculation_id: str
    label: Annotated[str, Field(min_length=1, max_length=120)]


class SubmitInput(RevisionInput):
    environment: Literal["sandbox", "production"] = "sandbox"
    scenario: Literal["accept", "reject", "timeout_after_accept"] = "accept"


class PaymentInput(Input):
    amount: PositiveMoney
    evidence_id: str
    note: Text


class ConsultInput(Input):
    question: Annotated[str, Field(min_length=2, max_length=4000)]
    authority_ids: Annotated[list[str], Field(max_length=10)] = []
    as_of: date


class ConsultOutput(Input):
    answer: Annotated[str, Field(min_length=1, max_length=10000)]
    citations: Annotated[list[str], Field(max_length=10)] = []
    missing_facts: Annotated[list[str], Field(max_length=20)] = []
    risk: Literal["low", "medium", "high", "unknown"] = "unknown"


class CSVInput(Input):
    csv_text: Annotated[str, Field(min_length=1, max_length=1_500_000)]

class ReverseInput(RevisionInput):
    event_date: date
