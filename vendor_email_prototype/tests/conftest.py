from __future__ import annotations
import base64
import hashlib
import hmac
import time
from typing import Dict, Generator, Union
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
from unitpost._webhooks import _secret_key_bytes
from app.config import settings
from app.database import Base, get_db
from app.email_provider import MockProvider, get_mock_provider
from app.main import app

TEST_SECRET = "whsec_testsecretkey12345678901234567890123="

@pytest.fixture(scope="session", autouse=True)
def configure_test_settings():
    settings.ENV = "development"
    settings.EMAIL_PROVIDER = "mock"
    settings.UNITPOST_WEBHOOK_SECRET = TEST_SECRET
    settings.TEST_ALLOWLIST = ["tester@agency.example.com", "admin@agency.example.com"]
    settings.DAILY_LIMIT = 200
    settings.BATCH_SIZE = 50


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def mock_provider() -> MockProvider:
    provider = get_mock_provider()
    provider.contacts.clear()
    provider.campaigns.clear()
    provider.suppressions.clear()
    provider.sent_emails.clear()
    provider.stats.clear()
    return provider


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def generate_svix_headers(
    payload: Union[bytes, str],
    secret: str = TEST_SECRET,
    msg_id: str = "msg_test_001",
    timestamp: int = None,
) -> Dict[str, str]:
    """Helper to generate valid Svix HMAC-SHA256 headers for Unitpost webhooks."""
    if timestamp is None:
        timestamp = int(time.time())

    body_str = payload.decode() if isinstance(payload, bytes) else payload
    signed_content = f"{msg_id}.{timestamp}.{body_str}".encode()

    secret_bytes = _secret_key_bytes(secret)
    sig = base64.b64encode(hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()).decode()

    return {
        "svix-id": msg_id,
        "svix-timestamp": str(timestamp),
        "svix-signature": f"v1,{sig}",
    }
