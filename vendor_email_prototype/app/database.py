from __future__ import annotations
import datetime
from typing import Generator, Optional
from sqlalchemy import create_engine, String, Text, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker, Session, Mapped, mapped_column
from app.config import settings

# SQLite connection args for thread safety; Postgres-ready syntax
connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

class Suppression(Base):
    """
    Permanent table for suppressed addresses.
    Reasons: unsubscribe, bounce, complaint, manual.
    Never delete rows via normal operations.
    """
    __tablename__ = "suppression"

    email: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)  # unsubscribe, bounce, complaint, manual
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # webhook, csv_import, manual_admin
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class SuppressionAudit(Base):
    """
    Audit table logging any administrative modifications or re-subscriptions.
    """
    __tablename__ = "suppression_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # unsuppressed, manually_suppressed
    reason: Mapped[str] = mapped_column(Text, nullable=False)       # Mandatory justification
    admin_user: Mapped[str] = mapped_column(String(100), default="admin", nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class WebhookEvent(Base):
    """
    Stores processed webhook event_ids for strict idempotency.
    """
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class Contact(Base):
    """
    Vendor contacts imported for outreach.
    """
    __tablename__ = "contacts"

    email: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    consent_source: Mapped[str] = mapped_column(String(255), nullable=False)
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class CampaignCheckpoint(Base):
    """
    Tracks state per (campaign, contact) so crashes never double-send.
    """
    __tablename__ = "campaign_checkpoint"

    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)  # f"{campaign_id}:{email}"
    campaign_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # queued, sent, failed, skipped
    batch_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CampaignState(Base):
    """
    Overall campaign state, counters, and pause status.
    """
    __tablename__ = "campaign_state"

    campaign_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)  # draft, sending, paused, completed
    total_recipients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unitpost_campaign_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
