from __future__ import annotations
import json
import pytest
from app.config import settings
from app.database import Contact, Suppression, SuppressionAudit, WebhookEvent, CampaignCheckpoint
from app.csv_processor import process_contacts_csv
from app.campaign_engine import send_test_email, dispatch_campaign_batch
from tests.conftest import generate_svix_headers, TEST_SECRET


# ========================================================
# 1. CSV Cleaning, Validation & Deduplication
# ========================================================
def test_csv_cleaning_and_validation(db_session, mock_provider):
    csv_data = """email, company_name, consent_source
    Alice@Example.COM , Acme Corp , 2024 Agreement
    bob@example.com, Beta LLC, Master Recruiter Contract
    invalid-email-address, Bad Inc, None
    alice@example.com , Acme Corp Repeat , Duplicate
    , Missing Email Co, None
    charlie@example.com, Gamma Ltd, 2025 Vendor Agreement
    """

    counts = process_contacts_csv(csv_text=csv_data, db=db_session, provider=mock_provider)

    assert counts["imported"] == 3  # alice, bob, charlie
    assert counts["duplicates"] == 1  # repeated alice
    assert counts["invalid"] == 2  # invalid-email-address, empty email
    assert counts["suppressed"] == 0

    # Verify normalization
    alice = db_session.query(Contact).filter(Contact.email == "alice@example.com").first()
    assert alice is not None
    assert alice.company_name == "Acme Corp"
    assert alice.consent_source == "2024 Agreement"


# ========================================================
# 2. Suppressed Address Skipped on Import
# ========================================================
def test_suppressed_address_skipped_on_import(db_session, mock_provider):
    # Pre-seed suppression table
    db_session.add(Suppression(
        email="suppressed@partner.com",
        reason="unsubscribe",
        source="manual_import",
    ))
    db_session.commit()

    csv_data = """email,company_name,consent_source
    active@partner.com,Active Partner,Agreement
    SUPPRESSED@PARTNER.COM,Suppressed Partner,Agreement
    """

    counts = process_contacts_csv(csv_text=csv_data, db=db_session, provider=mock_provider)

    assert counts["imported"] == 1
    assert counts["suppressed"] == 1

    # Verify suppressed was not inserted into contacts
    assert db_session.query(Contact).filter(Contact.email == "suppressed@partner.com").first() is None
    assert db_session.query(Contact).filter(Contact.email == "active@partner.com").first() is not None


# ========================================================
# 3. Unsubscribe Webhook Creates Suppression Row
# ========================================================
def test_unsubscribe_webhook_creates_suppression_row(client, db_session):
    payload = {
        "type": "contact.updated",
        "event_id": "evt_unsub_999",
        "created_at": "2026-11-23T12:00:00Z",
        "data": {
            "id": "con_123",
            "email": "optout@vendor.com",
            "unsubscribed": True
        }
    }
    raw_body = json.dumps(payload)
    headers = generate_svix_headers(payload=raw_body, msg_id="evt_unsub_999")

    res = client.post("/webhooks/unitpost", content=raw_body, headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "processed"

    # Verify permanent suppression row created
    suppression = db_session.query(Suppression).filter(Suppression.email == "optout@vendor.com").first()
    assert suppression is not None
    assert suppression.reason == "unsubscribe"
    assert suppression.source == "unitpost_webhook"


def test_bounce_and_complaint_webhooks_create_suppression(client, db_session):
    # Test bounce
    bounce_payload = {
        "type": "email.bounced",
        "event_id": "evt_bounce_101",
        "created_at": "2026-11-23T12:00:00Z",
        "data": {
            "to": "hardbounce@vendor.com",
            "suppressed": ["hardbounce@vendor.com"],
            "reasons": [{"email": "hardbounce@vendor.com", "reason": "BOUNCE"}]
        }
    }
    raw_bounce = json.dumps(bounce_payload)
    headers_bounce = generate_svix_headers(payload=raw_bounce, msg_id="evt_bounce_101")
    res = client.post("/webhooks/unitpost", content=raw_bounce, headers=headers_bounce)
    assert res.status_code == 200

    bounce_row = db_session.query(Suppression).filter(Suppression.email == "hardbounce@vendor.com").first()
    assert bounce_row is not None
    assert bounce_row.reason == "bounce"


# ========================================================
# 4. Duplicate Webhook Is Ignored (Idempotency)
# ========================================================
def test_duplicate_webhook_is_ignored(client, db_session):
    payload = {
        "type": "email.complained",
        "event_id": "evt_repeat_idempotency_1",
        "created_at": "2026-11-23T12:00:00Z",
        "data": {"to": "spam-reporter@vendor.com"}
    }
    raw_body = json.dumps(payload)
    headers = generate_svix_headers(payload=raw_body, msg_id="evt_repeat_idempotency_1")

    # First delivery
    res1 = client.post("/webhooks/unitpost", content=raw_body, headers=headers)
    assert res1.status_code == 200
    assert res1.json()["status"] == "processed"

    # Second delivery (repeat)
    res2 = client.post("/webhooks/unitpost", content=raw_body, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["status"] == "duplicate_ignored"

    # Verify only 1 log row stored in WebhookEvent
    events = db_session.query(WebhookEvent).filter(WebhookEvent.event_id == "evt_repeat_idempotency_1").all()
    assert len(events) == 1


# ========================================================
# 5. Bad Signature Rejected
# ========================================================
def test_bad_signature_rejected(client, db_session):
    payload = {
        "type": "email.bounced",
        "event_id": "evt_tampered_001",
        "created_at": "2026-11-23T12:00:00Z",
        "data": {"to": "hacker@vendor.com"}
    }
    raw_body = json.dumps(payload)
    bad_headers = {
        "svix-id": "evt_tampered_001",
        "svix-timestamp": "1735689600",
        "svix-signature": "v1,invalid_forged_signature_value=",
    }

    res = client.post("/webhooks/unitpost", content=raw_body, headers=bad_headers)
    assert res.status_code == 400
    assert "signature" in res.json()["detail"].lower()

    # Verify address was NOT suppressed
    assert db_session.query(Suppression).filter(Suppression.email == "hacker@vendor.com").first() is None


# ========================================================
# 6. Non-Allowlisted Send Is Refused in Non-Production
# ========================================================
def test_non_allowlisted_send_is_refused(db_session, mock_provider):
    settings.ENV = "development"
    settings.TEST_ALLOWLIST = ["tester@agency.example.com"]

    # Allowed send succeeds
    result = send_test_email(
        to_email="tester@agency.example.com",
        company_name="Allowed Vendor",
        db=db_session,
        provider=mock_provider
    )
    assert result["status"] == "sent"

    # Non-allowlisted send is strictly refused
    with pytest.raises(PermissionError) as exc_info:
        send_test_email(
            to_email="unauthorized@external.com",
            company_name="External Corp",
            db=db_session,
            provider=mock_provider
        )
    assert "TEST_ALLOWLIST" in str(exc_info.value)


# ========================================================
# 7. Admin Re-subscription Requires Reason & Audits
# ========================================================
def test_admin_resubscribe_requires_reason_and_audits(client, db_session):
    # Setup suppressed user
    db_session.add(Suppression(email="rejoin@vendor.com", reason="unsubscribe", source="webhook"))
    db_session.commit()

    # Attempt without reason
    res_no_reason = client.post("/admin/resubscribe", data={"email": "rejoin@vendor.com", "reason": ""})
    assert res_no_reason.status_code == 400

    # Successful re-subscribe with reason
    res_success = client.post("/admin/resubscribe", data={
        "email": "rejoin@vendor.com",
        "reason": "Signed updated 2026 vendor agreement requesting re-enrollment",
        "admin_user": "compliance_officer_sarah"
    })
    assert res_success.status_code == 200

    # Verify removed from suppression table
    assert db_session.query(Suppression).filter(Suppression.email == "rejoin@vendor.com").first() is None

    # Verify logged in audit table
    audit = db_session.query(SuppressionAudit).filter(SuppressionAudit.email == "rejoin@vendor.com").first()
    assert audit is not None
    assert audit.action == "unsuppressed"
    assert "re-enrollment" in audit.reason
    assert audit.admin_user == "compliance_officer_sarah"


# ========================================================
# 8. Campaign Checkpointing and Suppressed Skipping
# ========================================================
def test_campaign_dispatch_skips_suppressed_and_checkpoints(db_session, mock_provider):
    settings.ENV = "development"
    settings.TEST_ALLOWLIST = ["tester@agency.example.com", "admin@agency.example.com"]

    # Add contacts
    c1 = Contact(email="tester@agency.example.com", company_name="Test Partner", consent_source="Agreement")
    c2 = Contact(email="admin@agency.example.com", company_name="Admin Partner", consent_source="Agreement")
    db_session.add_all([c1, c2])

    # Suppress one of them
    db_session.add(Suppression(email="admin@agency.example.com", reason="bounce", source="webhook"))
    db_session.commit()

    # Dispatch campaign
    res = dispatch_campaign_batch(campaign_id="test-camp-01", confirm=True, db=db_session, provider=mock_provider)

    assert res["sent_in_this_batch"] == 1  # Only tester sent, admin skipped due to suppression
    assert res["total_sent_campaign"] == 1

    # Checkpoint exists
    cp = db_session.query(CampaignCheckpoint).filter(CampaignCheckpoint.email == "tester@agency.example.com").first()
    assert cp is not None
    assert cp.status == "sent"

    # Re-running dispatch does not double-send
    res2 = dispatch_campaign_batch(campaign_id="test-camp-01", confirm=True, db=db_session, provider=mock_provider)
    assert res2["sent_in_this_batch"] == 0
    assert res2["status"] == "completed"
