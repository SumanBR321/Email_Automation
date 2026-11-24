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


# Global singleton instance for the app runtime
_mock_instance: Optional[MockProvider] = None

def get_email_provider() -> EmailProvider:
    global _mock_instance
    if settings.EMAIL_PROVIDER.lower() == "unitpost":
        return UnitpostProvider()
    if _mock_instance is None:
        _mock_instance = MockProvider()
    return _mock_instance

def get_mock_provider() -> MockProvider:
    global _mock_instance
    if _mock_instance is None:
        _mock_instance = MockProvider()
    return _mock_instance
