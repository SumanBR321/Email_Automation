from __future__ import annotations
import csv
import io
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy.orm import Session
from app.database import Contact, Suppression
from app.email_provider import EmailProvider

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

EMAIL_HEADER_ALIASES = {
    "email", "email address", "email_address", "e-mail", "e_mail",
    "mail", "contact_email", "contact email", "recipient", "recipient_email",
    "vendor_email", "vendor email"
}

COMPANY_HEADER_ALIASES = {
    "company", "company_name", "company name", "organization", "org",
    "business", "business_name", "vendor", "vendor_name", "account",
    "account_name", "firm", "client"
}

CONSENT_HEADER_ALIASES = {
    "consent", "consent_source", "consent source", "source", "agreement",
    "contract", "consent_type", "optin_source", "opt_in_source"
}


def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    return bool(EMAIL_REGEX.match(email.strip()))


def normalize_header(name: str) -> str:
    """Strips quotes, spaces, and normalizes delimiters."""
    cleaned = name.strip().strip("'\"").lower()
    return re.sub(r"[\s\-_]+", " ", cleaned)


def detect_dialect_and_read(csv_text: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Robust reader that detects delimiters (comma, semicolon, tab)
    and handles missing or aliased headers dynamically.
    """
    sample = csv_text[:4096]
    delimiter = ","
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except Exception:
        # Fallback: check most common delimiter in first line
        first_line = csv_text.splitlines()[0] if csv_text.splitlines() else ""
        if "\t" in first_line:
            delimiter = "\t"
        elif ";" in first_line:
            delimiter = ";"
        elif "|" in first_line:
            delimiter = "|"

    stream = io.StringIO(csv_text)
    reader = csv.reader(stream, delimiter=delimiter)
    all_rows = [row for row in reader if any(field.strip() for field in row)]

    if not all_rows:
        return [], []

    first_row = [field.strip() for field in all_rows[0]]

    # Check if first row contains headers or if it's already data (contains @)
    has_header = False
    for field in first_row:
        norm = normalize_header(field)
        if norm in EMAIL_HEADER_ALIASES or norm in COMPANY_HEADER_ALIASES or norm in CONSENT_HEADER_ALIASES:
            has_header = True
            break

    if has_header:
        headers = first_row
        data_rows = all_rows[1:]
    else:
        # Generate virtual headers dynamically based on column index
        headers = [f"col_{i}" for i in range(len(first_row))]
        # If first column contains '@', assign virtual 'email'
        if any("@" in f for f in first_row):
            headers[0] = "email"
            if len(headers) > 1:
                headers[1] = "company_name"
            if len(headers) > 2:
                headers[2] = "consent_source"
        data_rows = all_rows

    dict_rows: List[Dict[str, str]] = []
    for row in data_rows:
        row_dict = {}
        for idx, h in enumerate(headers):
            val = row[idx].strip() if idx < len(row) else ""
            row_dict[h] = val
        dict_rows.append(row_dict)

    return dict_rows, headers


def process_contacts_csv(
    csv_text: str,
    db: Session,
    provider: EmailProvider
) -> Dict[str, int]:
    """
    Dynamically processes and imports vendor contacts from any CSV file.
    - Resolves headers dynamically across common industry aliases.
    - Strips/lowercases email and validates format.
    - Skips permanently suppressed addresses.
    - Prevents duplicates in batch and updates/skips existing records.
    - Syncs valid contacts to the EmailProvider.
    """
    rows, headers = detect_dialect_and_read(csv_text.strip())
    if not rows and not headers:
        raise ValueError("Uploaded CSV file is empty.")

    # Find the matching column for email, company, consent
    email_key: Optional[str] = None
    company_key: Optional[str] = None
    consent_key: Optional[str] = None

    for h in headers:
        norm = normalize_header(h)
        if not email_key and norm in EMAIL_HEADER_ALIASES:
            email_key = h
        elif not company_key and norm in COMPANY_HEADER_ALIASES:
            company_key = h
        elif not consent_key and norm in CONSENT_HEADER_ALIASES:
            consent_key = h

    # If no email column was matched by name, find the column where most values contain '@'
    if not email_key:
        candidate_scores = {h: 0 for h in headers}
        for r in rows[:20]:
            for h in headers:
                if "@" in r.get(h, ""):
                    candidate_scores[h] += 1
        best_col = max(candidate_scores, key=candidate_scores.get, default=None)
        if best_col and candidate_scores[best_col] > 0:
            email_key = best_col

    if not email_key:
        raise ValueError(
            "Could not detect an email column in the CSV. "
            "Please include a column named 'email', 'email address', or ensure contacts contain valid email addresses."
        )

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

    for row in rows:
        raw_email = row.get(email_key, "")
        if not raw_email:
            counts["invalid"] += 1
            continue

        email = raw_email.strip().lower()
        company_name = (row.get(company_key, "") if company_key else "") or "Valued Partner"
        consent_source = (row.get(consent_key, "") if consent_key else "") or "Candidate Sharing Agreement"

        company_name = company_name.strip()
        consent_source = consent_source.strip()

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
