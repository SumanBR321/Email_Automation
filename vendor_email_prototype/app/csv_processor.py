from __future__ import annotations
import csv
import io
import re
from typing import Any, Dict, List, Set
from sqlalchemy.orm import Session
from app.database import Contact, Suppression
from app.email_provider import EmailProvider

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    return bool(EMAIL_REGEX.match(email))


def process_contacts_csv(
    csv_text: str,
    db: Session,
    provider: EmailProvider
) -> Dict[str, int]:
    """
    Imports vendor contacts from CSV (email, company_name, consent_source).
    - Lowercase and trim emails.
    - Validates email format.
    - Deduplicates entries within the CSV and against existing DB.
    - Skips any address in the permanent local suppression table.
    - Reports: imported, duplicates, invalid, suppressed.
    """
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    
    # Normalize fieldnames to lowercase stripped
    if reader.fieldnames:
        fieldnames_map = {fn.strip().lower(): fn for fn in reader.fieldnames}
    else:
        fieldnames_map = {}

    email_col = fieldnames_map.get("email")
    company_col = fieldnames_map.get("company_name") or fieldnames_map.get("company")
    consent_col = fieldnames_map.get("consent_source") or fieldnames_map.get("consent")

    if not email_col:
        raise ValueError("CSV must contain an 'email' column header.")

    counts = {
        "imported": 0,
        "duplicates": 0,
        "invalid": 0,
        "suppressed": 0,
    }

    seen_in_batch: Set[str] = set()
    valid_contacts_to_save: List[Contact] = []
    contacts_payload: List[Dict[str, Any]] = []

    # Pre-fetch existing emails and suppressions in one query for speed
    existing_contacts = {c[0] for c in db.query(Contact.email).all()}
    local_suppressions = {s[0] for s in db.query(Suppression.email).all()}

    for row in reader:
        raw_email = row.get(email_col, "")
        if not raw_email:
            counts["invalid"] += 1
            continue

        email = raw_email.strip().lower()
        company_name = (row.get(company_col, "") or "Valued Partner").strip()
        consent_source = (row.get(consent_col, "") or "Candidate Sharing Agreement").strip()

        # 1. Format Validation
        if not is_valid_email(email):
            counts["invalid"] += 1
            continue

        # 2. Batch Duplication Check
        if email in seen_in_batch:
            counts["duplicates"] += 1
            continue
        seen_in_batch.add(email)

        # 3. Permanent Suppression Check
        if email in local_suppressions:
            counts["suppressed"] += 1
            continue

        # 4. Existing Database Duplicate Check
        if email in existing_contacts:
            counts["duplicates"] += 1
            continue

        # Valid contact
        contact = Contact(
            email=email,
            company_name=company_name,
            consent_source=consent_source,
        )
        valid_contacts_to_save.append(contact)
        contacts_payload.append({
            "email": email,
            "company_name": company_name,
            "consent_source": consent_source,
        })
        existing_contacts.add(email)

    # Persist in DB
    if valid_contacts_to_save:
        db.add_all(valid_contacts_to_save)
        db.commit()
        counts["imported"] = len(valid_contacts_to_save)

        # Sync to EmailProvider
        provider.import_contacts(contacts_payload)

    return counts
