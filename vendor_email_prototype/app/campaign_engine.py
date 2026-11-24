from __future__ import annotations
import datetime
import os
from typing import Any, Dict, List, Optional
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session
from app.config import settings
from app.database import CampaignCheckpoint, CampaignState, Contact, Suppression
from app.email_provider import EmailProvider

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)


def render_notice_html(company_name: str, unsubscribe_url: str = "#unsubscribe") -> str:
    template = jinja_env.get_template("email_notice.html")
    return template.render(
        company_name=company_name,
        agency_name=settings.AGENCY_NAME,
        agency_postal_address=settings.AGENCY_POSTAL_ADDRESS,
        unsubscribe_url=unsubscribe_url,
    )


def render_notice_text(company_name: str, unsubscribe_url: str = "#unsubscribe") -> str:
    template = jinja_env.get_template("email_notice.txt")
    return template.render(
        company_name=company_name,
        agency_name=settings.AGENCY_NAME,
        agency_postal_address=settings.AGENCY_POSTAL_ADDRESS,
        unsubscribe_url=unsubscribe_url,
    )


def send_test_email(
    to_email: str,
    company_name: str,
    db: Session,
    provider: EmailProvider
) -> Dict[str, Any]:
    """
    Sends a test email to a single recipient.
    Strictly verifies against TEST_ALLOWLIST if ENV != production.
    Checks permanent suppression before dispatch.
    """
    normalized_email = to_email.strip().lower()

    # 1. Allowlist enforcement in non-production
    if settings.ENV != "production":
        if normalized_email not in settings.TEST_ALLOWLIST:
            raise PermissionError(
                f"Send refused: Recipient '{normalized_email}' is not in TEST_ALLOWLIST {settings.TEST_ALLOWLIST}. "
                f"Set ENV=production or add this address to TEST_ALLOWLIST."
            )

    # 2. Local suppression check
    suppressed = db.query(Suppression).filter(Suppression.email == normalized_email).first()
    if suppressed:
        raise ValueError(f"Send refused: Address '{normalized_email}' is in suppression list ({suppressed.reason}).")

    subject = f"{settings.AGENCY_NAME} — Verified Technical Candidate Update"
    html_body = render_notice_html(company_name=company_name)

    res = provider.send_test(to_email=normalized_email, subject=subject, body=html_body)
    return {
        "status": "sent",
        "recipient": normalized_email,
        "provider_result": res,
    }


def dispatch_campaign_batch(
    campaign_id: str,
    confirm: bool,
    db: Session,
    provider: EmailProvider
) -> Dict[str, Any]:
    """
    Sends campaign notices in safe, checkpointed batches.
    - Syncs & enforces both local and remote suppressions.
    - Enforces TEST_ALLOWLIST in non-production.
    - Requires confirm=True in production.
    - Respects DAILY_LIMIT (200/day) and BATCH_SIZE (50) to protect sender reputation.
    - Checkpoints every send by (campaign, contact) so crashes never double-send.
    """
    # 1. Pre-send suppression sync: ensure local table reflects remote suppressions
    try:
        remote_supps = provider.list_suppressions()
        for s in remote_supps:
            rem_email = (s.get("email") or "").strip().lower()
            if rem_email:
                existing = db.query(Suppression).filter(Suppression.email == rem_email).first()
                if not existing:
                    db.add(Suppression(
                        email=rem_email,
                        reason=s.get("reason", "remote_sync"),
                        source="unitpost_remote_sync"
                    ))
        db.commit()
    except Exception:
        db.rollback()

    local_suppressions = {s[0] for s in db.query(Suppression.email).all()}

    # 2. Retrieve contacts and filter out suppressions
    all_contacts = db.query(Contact).all()
    unsuppressed_contacts = [c for c in all_contacts if c.email.lower().strip() not in local_suppressions]

    # 3. Non-production allowlist gate
    if settings.ENV != "production":
        eligible_contacts = [c for c in unsuppressed_contacts if c.email.lower().strip() in settings.TEST_ALLOWLIST]
    else:
        eligible_contacts = unsuppressed_contacts
        # Production confirmation gate
        if not confirm:
            return {
                "requires_confirmation": True,
                "eligible_recipients_count": len(eligible_contacts),
                "message": (
                    f"Production send confirmation required. "
                    f"Targeting {len(eligible_contacts)} eligible unsuppressed contacts. "
                    f"Pass confirm=True to initiate sending."
                ),
            }

    # 4. Check existing checkpoints for this campaign
    sent_checkpoints = {
        cp.email for cp in db.query(CampaignCheckpoint.email).filter(
            CampaignCheckpoint.campaign_id == campaign_id,
            CampaignCheckpoint.status == "sent"
        ).all()
    }
    pending_contacts = [c for c in eligible_contacts if c.email not in sent_checkpoints]

    if not pending_contacts:
        return {
            "status": "completed",
            "campaign_id": campaign_id,
            "sent_in_this_batch": 0,
            "total_sent_campaign": len(sent_checkpoints),
            "message": "All eligible recipients have already been sent."
        }

    # 5. Daily limit quota calculation
    today_start = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today_count = db.query(CampaignCheckpoint).filter(
        CampaignCheckpoint.sent_at >= today_start,
        CampaignCheckpoint.status == "sent"
    ).count()

    remaining_today = max(0, settings.DAILY_LIMIT - sent_today_count)
    if remaining_today <= 0:
        return {
            "status": "daily_limit_reached",
            "campaign_id": campaign_id,
            "sent_today": sent_today_count,
            "daily_limit": settings.DAILY_LIMIT,
            "message": f"Daily limit of {settings.DAILY_LIMIT} reached. Resume dispatch tomorrow."
        }

    # 6. Take next batch slice
    batch_to_send = pending_contacts[:min(settings.BATCH_SIZE, remaining_today)]
    subject = f"{settings.AGENCY_NAME} — Verified Technical Candidate Update"

    sent_now = 0
    errors: List[str] = []

    for idx, contact in enumerate(batch_to_send):
        idempotency_key = f"{campaign_id}:{contact.email}"
        checkpoint = db.query(CampaignCheckpoint).filter(
            CampaignCheckpoint.idempotency_key == idempotency_key
        ).first()

        if not checkpoint:
            checkpoint = CampaignCheckpoint(
                idempotency_key=idempotency_key,
                campaign_id=campaign_id,
                email=contact.email,
                status="queued",
                batch_index=idx,
            )
            db.add(checkpoint)
            db.commit()

        # Send individual message
        try:
            html_body = render_notice_html(company_name=contact.company_name)
            provider.send_test(to_email=contact.email, subject=subject, body=html_body)

            checkpoint.status = "sent"
            checkpoint.sent_at = datetime.datetime.now(datetime.timezone.utc)
            db.commit()
            sent_now += 1
        except Exception as e:
            checkpoint.status = "failed"
            checkpoint.error_message = str(e)
            db.commit()
            errors.append(f"Failed {contact.email}: {str(e)}")

    # Update or create CampaignState
    state = db.query(CampaignState).filter(CampaignState.campaign_id == campaign_id).first()
    if not state:
        state = CampaignState(
            campaign_id=campaign_id,
            name=f"Vendor Outreach {campaign_id}",
            status="sending",
            total_recipients=len(eligible_contacts),
            sent_count=len(sent_checkpoints) + sent_now,
        )
        db.add(state)
    else:
        state.sent_count = len(sent_checkpoints) + sent_now
        state.status = "completed" if len(pending_contacts) == sent_now else "sending"
    db.commit()

    return {
        "status": "in_progress" if len(pending_contacts) > sent_now else "completed",
        "campaign_id": campaign_id,
        "sent_in_this_batch": sent_now,
        "total_sent_campaign": state.sent_count,
        "remaining_unprocessed": len(pending_contacts) - sent_now,
        "errors": errors,
    }
