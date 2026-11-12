"""Native company identity candidates; never a financial confirmation."""
import json
from importlib.resources import files
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import Actor
from .db import DomainError, digest, now
from .service import Service

OWNER_REGISTRATION = json.loads(files("price").joinpath("rt_registration.json").read_text())


class CRMCompanyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crm_workspace_id: UUID
    price_company_id: str
    source_reference: str = Field(min_length=1)
    registered_by: Literal["omonyang-root"] = "omonyang-root"
    confirmed_by_human: bool = False
    confirmed_by: Literal["owner"] | None = None
    confirmation_ref: Literal["C0C6HJM0BV5:1791500798.801419"] | None = None

    @model_validator(mode="after")
    def owner_confirmation(self):
        if self.confirmed_by_human:
            if self.confirmed_by != "owner" or self.confirmation_ref is None:
                raise ValueError("Human confirmation requires the recorded owner decision")
            if (str(self.crm_workspace_id) != OWNER_REGISTRATION["crm_workspace_id"]
                    or self.price_company_id != OWNER_REGISTRATION["price_company_id"]
                    or self.source_reference != OWNER_REGISTRATION["source_reference"]):
                raise ValueError("Owner confirmation applies only to its approved native binding")
        elif self.confirmed_by is not None or self.confirmation_ref is not None:
            raise ValueError("Unconfirmed candidates cannot carry confirmation fields")
        return self


class CRMCompanyBindings:
    def __init__(self, service: Service):
        self.service = service
        self.db = service.db

    def register(self, actor: Actor, company: str, data: CRMCompanyCandidate) -> dict[str, object]:
        with self.db.transaction() as connection:
            self.service.auth.guard(connection, actor, company, ("owner",))
            if data.price_company_id != company:
                raise DomainError("CRM_COMPANY_MISMATCH", "등록 대상 회사가 다릅니다.", 404)
            current = self.db.list(connection, company, "crm_company_binding")
            workspace = str(data.crm_workspace_id)
            if current:
                if (len(current) == 1 and current[0].get("confirmed_by_human") == data.confirmed_by_human
                        and current[0]["crm_workspace_id"] == workspace
                        and current[0]["price_company_id"] == company
                        and current[0].get("registered_by") == data.registered_by
                        and current[0].get("source_reference") == data.source_reference
                        and current[0].get("confirmed_by") == data.confirmed_by
                        and current[0].get("confirmation_ref") == data.confirmation_ref):
                    return current[0]
                raise DomainError("CRM_BINDING_CONFLICT", "기존 연결을 덮어쓸 수 없습니다.", 409)
            row = self.db.create(connection, company, "crm_company_binding", {
                "crm_workspace_id": workspace,
                "price_company_id": company,
                "registered_by": data.registered_by,
                "registered_at": now(),
                "registered_actor_id": actor.id,
                "confirmed_by_human": data.confirmed_by_human,
                "confirmed_by": data.confirmed_by,
                "confirmation_ref": data.confirmation_ref,
                "source_reference": data.source_reference,
            }, ident="crm_company_binding_" + digest({"company": company}))
            self.db.audit(connection, company, actor.id, "crm_company_binding.registered",
                          row["id"], {"identity_only": True,
                                      "confirmed_by_human": data.confirmed_by_human,
                                      "confirmation_ref": data.confirmation_ref})
            return row

    def get(self, actor: Actor, company: str) -> dict[str, object]:
        with self.db.connect() as connection:
            self.service.auth.guard(connection, actor, company)
            rows = self.db.list(connection, company, "crm_company_binding")
            candidate = next(iter(rows), None)
            return {"candidate": candidate,
                    "confirmed_connection": bool(candidate and candidate.get("confirmed_by_human") is True)}
