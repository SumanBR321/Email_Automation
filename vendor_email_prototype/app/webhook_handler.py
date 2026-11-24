from __future__ import annotations
import json
from typing import Any, Dict
from sqlalchemy.orm import Session
from unitpost import verify_webhook, WebhookVerificationError
from app.config import settings
from app.database import Suppression, WebhookEvent

def handle_unitpost_webhook(
    raw_body: bytes,
    headers: Dict[str, str],
    db: Session
) -> Dict[str, Any]:
    """
    Verifies Unitpost webhook payload HMAC signature (Svix-compatible).
    Ensures idempotency by checking and storing event_id.
    Writes bounce, complaint, and unsubscribe events to the permanent suppression table.
    """
    # 1. Signature Verification
    try:
        event = verify_webhook(
            payload=raw_body,
            secret=settings.UNITPOST_WEBHOOK_SECRET,
            headers=headers,
        )
    except WebhookVerificationError as exc:
        raise ValueError(f"Webhook signature verification failed: {exc}")
    except Exception as exc:
        raise ValueError(f"Invalid webhook format or headers: {exc}")

    event_id = event.get("event_id") or headers.get("svix-id")
    if not event_id:
        raise ValueError("Missing required event_id in webhook payload.")

    # 2. Idempotency Check: if event_id already processed, ignore safely
    existing_event = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).first()
    if existing_event:
        return {
            "status": "duplicate_ignored",
            "event_id": event_id,
            "message": "Event already processed.",
        }

    # Record event in idempotency log
    event_type = event.get("type", "unknown")
    webhook_log = WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=raw_body.decode(errors="replace"),
    )
    db.add(webhook_log)

    data = event.get("data", {})
    action_taken = "none"

    # Helper to insert into suppression if not exists
    def add_suppression(email: str, reason: str):
        normalized = email.strip().lower()
        if not normalized:
            return
        existing = db.query(Suppression).filter(Suppression.email == normalized).first()
        if not existing:
            db.add(Suppression(
                email=normalized,
                reason=reason,
                source="unitpost_webhook",
            ))

    # 3. Handle Event Types
    if event_type == "email.bounced":
        recipients = data.get("suppressed") or [data.get("to")]
        for r in recipients:
            if r:
                add_suppression(r, "bounce")
        action_taken = "suppressed_bounce"

    elif event_type == "email.complained":
        recipient = data.get("to")
        if recipient:
            add_suppression(recipient, "complaint")
        action_taken = "suppressed_complaint"

    elif event_type in ("contact.updated", "contact.created"):
        if data.get("unsubscribed") is True:
            recipient = data.get("email")
            if recipient:
                add_suppression(recipient, "unsubscribe")
            action_taken = "suppressed_unsubscribe"

    elif event_type == "topic.unsubscribed":
        recipient = data.get("email") or data.get("to")
        if recipient:
            add_suppression(recipient, "unsubscribe")
        action_taken = "suppressed_topic_unsubscribe"

    elif event_type == "email.suppressed":
        recipient = data.get("to")
        if recipient:
            add_suppression(recipient, "unsubscribe")
        action_taken = "suppressed_provider_exclusion"

    db.commit()
    return {
        "status": "processed",
        "event_id": event_id,
        "event_type": event_type,
        "action": action_taken,
    }
