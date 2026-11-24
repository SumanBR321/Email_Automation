from __future__ import annotations
import abc
import uuid
import datetime
from typing import Any, Dict, List, Optional
from app.config import settings

class EmailProvider(abc.ABC):
    """
    Abstract interface for email operations.
    Supports Unitpost and an in-memory MockProvider for testing without network.
    """

    @abc.abstractmethod
    def import_contacts(self, contacts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Import contacts with custom fields (e.g. consent_source, company_name)."""
        pass

    @abc.abstractmethod
    def create_campaign(
        self,
        name: str,
        subject: str,
        template_content: str,
        recipients: List[str],
        topic_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a marketing campaign with an associated template and recipients."""
        pass

    @abc.abstractmethod
    def send_test(self, to_email: str, subject: str, body: str) -> Dict[str, Any]:
        """Send a single test message (restricted by allowlist rules)."""
        pass

    @abc.abstractmethod
    def send_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Trigger campaign dispatch."""
        pass

    @abc.abstractmethod
    def pause_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Pause a sending campaign."""
        pass

    @abc.abstractmethod
    def get_stats(self, campaign_id: str) -> Dict[str, Any]:
        """Retrieve delivery stats (queued, sent, delivered, bounced, complained, unsubscribed)."""
        pass

    @abc.abstractmethod
    def list_suppressions(self) -> List[Dict[str, Any]]:
        """List remote provider suppressions."""
        pass


class MockProvider(EmailProvider):
    """
    Deterministic in-memory mock provider with zero network activity.
    """

    def __init__(self) -> None:
        self.contacts: Dict[str, Dict[str, Any]] = {}
        self.campaigns: Dict[str, Dict[str, Any]] = {}
        self.suppressions: List[Dict[str, Any]] = []
        self.sent_emails: List[Dict[str, Any]] = []
        self.stats: Dict[str, Dict[str, int]] = {}

    def import_contacts(self, contacts: List[Dict[str, Any]]) -> Dict[str, Any]:
        for c in contacts:
            email = c["email"].lower().strip()
            self.contacts[email] = c
        return {"imported": len(contacts), "status": "completed"}

    def create_campaign(
        self,
        name: str,
        subject: str,
        template_content: str,
        recipients: List[str],
        topic_id: Optional[str] = None
    ) -> Dict[str, Any]:
        campaign_id = f"mock_cmp_{uuid.uuid4().hex[:8]}"
        self.campaigns[campaign_id] = {
            "id": campaign_id,
            "name": name,
            "subject": subject,
            "template_content": template_content,
            "recipients": recipients,
            "topic_id": topic_id,
            "status": "draft",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.stats[campaign_id] = {
            "queued": len(recipients),
            "sent": 0,
            "delivered": 0,
            "bounced": 0,
            "complained": 0,
            "unsubscribed": 0,
        }
        return {"id": campaign_id, "name": name, "status": "draft", "recipients_count": len(recipients)}

    def send_test(self, to_email: str, subject: str, body: str) -> Dict[str, Any]:
        email_id = f"mock_msg_{uuid.uuid4().hex[:8]}"
        self.sent_emails.append({
            "id": email_id,
            "to": to_email,
            "subject": subject,
            "body": body,
            "sent_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        return {"id": email_id, "to": to_email, "status": "sent"}

    def send_campaign(self, campaign_id: str) -> Dict[str, Any]:
        if campaign_id not in self.campaigns:
            raise ValueError(f"Campaign {campaign_id} not found")
        campaign = self.campaigns[campaign_id]
        campaign["status"] = "sending"
        recipients_count = len(campaign["recipients"])

        # In mock mode, mark as sent/delivered deterministically
        self.stats[campaign_id]["queued"] = 0
        self.stats[campaign_id]["sent"] = recipients_count
        self.stats[campaign_id]["delivered"] = recipients_count

        return {"id": campaign_id, "status": "sending", "recipients_count": recipients_count}

    def pause_campaign(self, campaign_id: str) -> Dict[str, Any]:
        if campaign_id not in self.campaigns:
            raise ValueError(f"Campaign {campaign_id} not found")
        self.campaigns[campaign_id]["status"] = "paused"
        return {"id": campaign_id, "status": "paused"}

    def get_stats(self, campaign_id: str) -> Dict[str, Any]:
        return self.stats.get(campaign_id, {
            "queued": 0,
            "sent": 0,
            "delivered": 0,
            "bounced": 0,
            "complained": 0,
            "unsubscribed": 0,
        })

    def list_suppressions(self) -> List[Dict[str, Any]]:
        return list(self.suppressions)

    def add_mock_suppression(self, email: str, reason: str = "unsubscribe") -> None:
        """Helper to seed remote suppression for testing."""
        self.suppressions.append({
            "email": email.lower().strip(),
            "reason": reason,
            "created_at": datetime.datetime.utcnow().isoformat(),
        })


class UnitpostProvider(EmailProvider):
    """
    Live Unitpost provider using the official 'unitpost' SDK.
    Uses campaigns to ensure hosted unsubscribe pages and topic preferences apply.
    """

    def __init__(self) -> None:
        from unitpost import Unitpost
        self.client = Unitpost(api_key=settings.UNITPOST_API_KEY)

    def import_contacts(self, contacts: List[Dict[str, Any]]) -> Dict[str, Any]:
        imported_count = 0
        for c in contacts:
            res = self.client.contacts.create({
                "email": c["email"],
                "custom_fields": {
                    "company_name": c.get("company_name", ""),
                    "consent_source": c.get("consent_source", ""),
                }
            })
            if not res.error:
                imported_count += 1
        return {"imported": imported_count, "status": "completed"}

    def create_campaign(
        self,
        name: str,
        subject: str,
        template_content: str,
        recipients: List[str],
        topic_id: Optional[str] = None
    ) -> Dict[str, Any]:
        # 1. Create reusable template
        template_res = self.client.templates.create({
            "name": f"Template - {name} ({uuid.uuid4().hex[:6]})",
            "subject": subject,
            "html": template_content,
            "category": "marketing",
        })
        if template_res.error:
            raise RuntimeError(f"Unitpost template creation failed: {template_res.error}")

        template_id = template_res.data["id"]

        # 2. Create campaign referencing template and inline recipients (up to 5,000)
        campaign_payload: Dict[str, Any] = {
            "name": name,
            "template_id": template_id,
            "from": settings.UNITPOST_FROM_EMAIL,
            "subject": subject,
            "recipients": recipients,
        }
        if topic_id or settings.UNITPOST_TOPIC_ID:
            campaign_payload["topic_id"] = topic_id or settings.UNITPOST_TOPIC_ID

        camp_res = self.client.campaigns.create(campaign_payload)
        if camp_res.error:
            raise RuntimeError(f"Unitpost campaign creation failed: {camp_res.error}")

        return camp_res.data

    def send_test(self, to_email: str, subject: str, body: str) -> Dict[str, Any]:
        # Unitpost transactional test send
        res = self.client.email.send({
            "from": settings.UNITPOST_FROM_EMAIL,
            "to": to_email,
            "subject": subject,
            "html": body,
        })
        if res.error:
            raise RuntimeError(f"Unitpost test send failed: {res.error}")
        return res.data

    def send_campaign(self, campaign_id: str) -> Dict[str, Any]:
        res = self.client.campaigns.send(campaign_id)
        if res.error:
            raise RuntimeError(f"Unitpost campaign send failed: {res.error}")
        return res.data

    def pause_campaign(self, campaign_id: str) -> Dict[str, Any]:
        res = self.client.campaigns.pause(campaign_id)
        if res.error:
            raise RuntimeError(f"Unitpost campaign pause failed: {res.error}")
        return res.data

    def get_stats(self, campaign_id: str) -> Dict[str, Any]:
        res = self.client.campaigns.stats(campaign_id)
        if res.error:
            return {"queued": 0, "sent": 0, "delivered": 0, "bounces": 0, "complaints": 0, "unsubscribed": 0}
        data = res.data or {}
        return {
            "queued": data.get("queued", 0),
            "sent": data.get("sent", 0),
            "delivered": data.get("delivered", 0),
            "bounces": data.get("bounces", 0),
            "complaints": data.get("complaints", 0),
            "unsubscribed": data.get("unsubscribes", 0),
        }

    def list_suppressions(self) -> List[Dict[str, Any]]:
        suppressed_items = []
        for item in self.client.suppressions.list_all():
            suppressed_items.append({
                "email": item.get("email"),
                "reason": item.get("reason"),
                "created_at": item.get("created_at"),
            })
        return suppressed_items


class SmtpProvider(EmailProvider):
    """
    Direct SMTP email provider using Python's built-in smtplib and ssl.
    Enables live delivery without requiring custom domain DNS verification.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        use_ssl: Optional[bool] = None,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
    ) -> None:
        # Load fresh settings in case .env was updated without a server restart
        from app.config import Settings
        cfg = Settings()

        self.host = host or cfg.SMTP_HOST
        self.port = port if port is not None else cfg.SMTP_PORT
        self.user = user or cfg.SMTP_USER
        self.password = password or cfg.SMTP_PASSWORD
        self.use_ssl = use_ssl if use_ssl is not None else cfg.SMTP_USE_SSL
        self.from_email = from_email or cfg.SMTP_FROM_EMAIL or self.user
        self.from_name = from_name or cfg.SMTP_FROM_NAME or cfg.AGENCY_NAME
        self.sent_emails: List[Dict[str, Any]] = []

    def import_contacts(self, contacts: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"imported": len(contacts), "status": "completed"}

    def create_campaign(
        self,
        name: str,
        subject: str,
        template_content: str,
        recipients: List[str],
        topic_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        campaign_id = f"smtp_cmp_{uuid.uuid4().hex[:8]}"
        return {"id": campaign_id, "name": name, "status": "ready", "recipients_count": len(recipients)}

    def send_test(self, to_email: str, subject: str, body: str) -> Dict[str, Any]:
        import re
        import smtplib
        import ssl
        from email.message import EmailMessage

        if not self.user or not self.password:
            raise ValueError(
                "SMTP configuration missing: Please set SMTP_USER and SMTP_PASSWORD in your .env file."
            )

        msg = EmailMessage()
        msg["Subject"] = subject
        sender_header = f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email
        msg["From"] = sender_header
        msg["To"] = to_email

        # If body contains html, provide both plain text and rich HTML alternatives
        if "<html" in body.lower() or "<p" in body.lower() or "<div" in body.lower():
            plain_text = re.sub(r"<[^>]+>", "", body).strip()
            msg.set_content(plain_text or "Please view this email in an HTML-compatible client.")
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)

        message_id = f"smtp_{uuid.uuid4().hex[:12]}"

        # Connect and send
        if self.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.host, self.port, context=context) as server:
                server.login(self.user, self.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(self.user, self.password)
                server.send_message(msg)

        record = {
            "id": message_id,
            "to": to_email,
            "status": "sent",
            "sent_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.sent_emails.append(record)
        return record

    def send_campaign(self, campaign_id: str) -> Dict[str, Any]:
        return {"id": campaign_id, "status": "sending"}

    def pause_campaign(self, campaign_id: str) -> Dict[str, Any]:
        return {"id": campaign_id, "status": "paused"}

    def get_stats(self, campaign_id: str) -> Dict[str, Any]:
        return {
            "queued": 0,
            "sent": len(self.sent_emails),
            "delivered": len(self.sent_emails),
            "bounced": 0,
            "complained": 0,
            "unsubscribed": 0,
        }

    def list_suppressions(self) -> List[Dict[str, Any]]:
        return []


# Global singleton instance for the app runtime
_mock_instance: Optional[MockProvider] = None

def get_email_provider() -> EmailProvider:
    global _mock_instance
    from app.config import Settings
    cfg = Settings()
    provider_name = cfg.EMAIL_PROVIDER.lower().strip()
    if provider_name == "unitpost":
        return UnitpostProvider()
    elif provider_name == "smtp":
        return SmtpProvider()
    if _mock_instance is None:
        _mock_instance = MockProvider()
    return _mock_instance

def get_mock_provider() -> MockProvider:
    global _mock_instance
    if _mock_instance is None:
        _mock_instance = MockProvider()
    return _mock_instance
