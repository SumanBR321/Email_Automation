from __future__ import annotations
from typing import List, Optional, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "development"
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8001
    SECRET_KEY: str = "default_development_secret_key"

    # Provider Configuration: 'mock', 'unitpost', or 'smtp'
    EMAIL_PROVIDER: str = "mock"

    # SMTP Configuration (Direct smtplib relay, e.g. Gmail)
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_SSL: bool = True
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "Apex Talent Partners"

    # Unitpost Credentials (Send & Contacts scoped key, never full-access)
    UNITPOST_API_KEY: str = "pk_test_placeholder"
    UNITPOST_WEBHOOK_SECRET: str = "whsec_placeholder"
    UNITPOST_FROM_EMAIL: str = "notices@agency.example.com"
    UNITPOST_TOPIC_ID: Optional[str] = None

    TEST_ALLOWLIST: Union[str, List[str]] = ["tester@agency.example.com", "admin@agency.example.com"]

    AGENCY_NAME: str = "Apex Talent Partners"
    AGENCY_POSTAL_ADDRESS: str = "100 Montgomery St, Suite 1500, San Francisco, CA 94104"

    DAILY_LIMIT: int = 200
    BATCH_SIZE: int = 50

    DATABASE_URL: str = "sqlite:///./vendor_email.db"

    @field_validator("TEST_ALLOWLIST")
    @classmethod
    def parse_allowlist(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [addr.strip().lower() for addr in v.split(",") if addr.strip()]
        if isinstance(v, list):
            return [str(addr).strip().lower() for addr in v if str(addr).strip()]
        return []

settings = Settings()
