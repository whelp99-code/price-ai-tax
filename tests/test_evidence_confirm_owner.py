from pathlib import Path
import base64
from price.db import DomainError
from price.models import EvidenceCandidateLinkInput, EvidenceLinkConfirmInput
import pytest


def test_only_owner_can_confirm_evidence_link(w):
    encoded = base64.b64encode(b"SYNTHETIC EXTRA EVIDENCE - NOT REAL TAX DATA").decode()
    extra = w.svc.upload_evidence(w.owner, w.company, "extra.txt", encoded)
    tx = w.tx()
    linked = w.svc.link_evidence_candidate(
        w.preparer,
        w.company,
        tx["id"],
        EvidenceCandidateLinkInput(expected_revision=tx["revision"], evidence_id=extra["id"], reason="후보만 연결"),
    )
    assert linked["evidence_links"][0]["status"] == "candidate"
    payload = EvidenceLinkConfirmInput(expected_revision=linked["revision"], reason="확정")
    with pytest.raises(DomainError) as preparer:
        w.svc.confirm_evidence_link(w.preparer, w.company, tx["id"], extra["id"], payload)
    assert preparer.value.status == 403
    with pytest.raises(DomainError) as reviewer:
        w.svc.confirm_evidence_link(w.reviewer, w.company, tx["id"], extra["id"], payload)
    assert reviewer.value.status == 403
    confirmed = w.svc.confirm_evidence_link(w.owner, w.company, tx["id"], extra["id"], payload)
    assert confirmed["evidence_links"][0]["status"] == "confirmed"
    assert extra["id"] in confirmed["evidence_ids"]
def test_transaction_detail_hides_confirm_controls_unless_owner():
    text = Path("price/static/app.js").read_text(encoding="utf-8")
    confirm = "role()==='owner'?`<form data-link-action=\"confirm\"><label>확정 사유"
    assert confirm in text
    assert "writable()?`<button type=\"button\" class=\"small secondary\" data-link-action=\"unlink\">후보 해제</button><form data-link-action=\"confirm\">" not in text
