# Vendor Email Module Prototype

> Compliance-first vendor candidate notification service powered by **FastAPI** and **Unitpost** (with SQLite prototype and Postgres-ready schema).

This module delivers a one-time notice to ~4,000 vendor partner companies under active candidate-sharing agreements. It is built strictly to guarantee legal compliance with unsubscribes, spam complaints, and bounce suppressions.

---

## 🔒 Architectural & Compliance Guarantees

1. **Strict Repository Isolation**:
   - Resides exclusively in `vendor_email_prototype/`.
   - Leaves both `joblyst` (frontend) and `joblyst-backend` (scraper backend) completely untouched.

2. **Permanent Local Suppression List**:
   - Table `suppression` (`email`, `reason`, `source`, `created_at`).
   - Normal operations **never delete rows**.
   - Re-subscribing is restricted to an authenticated admin endpoint (`POST /admin/resubscribe`), requires a mandatory written legal reason, and logs to `suppression_audit`.
   - Checked locally on CSV import **and** before every email dispatch. Also syncs remote Unitpost workspace suppressions.

3. **Svix-Compatible Webhook Verification & Idempotency**:
   - Endpoint: `POST /webhooks/unitpost`.
   - Verifies HMAC-SHA256 signature using `UNITPOST_WEBHOOK_SECRET` over `${svix-id}.${svix-timestamp}.${raw_body}`. Rejects tampered payloads.
   - Enforces idempotency via `webhook_events(event_id)` so duplicate deliveries are safely ignored.
   - Automatically writes `contact.updated` (`unsubscribed: True`), `topic.unsubscribed`, `email.bounced`, and `email.complained` to `suppression`.

4. **Safety Gates & 200/Day Rate Protection**:
   - Non-production (`ENV != "production"`) strictly refuses to mail any address not listed in `TEST_ALLOWLIST`.
   - Production (`ENV == "production"`) requires `confirm=True` and surfaces recipient counts before sending.
   - Checkpoints every dispatch by `(campaign_id, email)` so crashes or server restarts never double-send.
   - Enforces `DAILY_LIMIT=200` and `BATCH_SIZE=50` to safeguard sender reputation on Unitpost free plans.

---

## 🛠️ Quickstart & Local Setup

### 1. Prerequisites
- Python 3.10+ (Tested on Python 3.13)
- Windows PowerShell or Unix Shell

### 2. Virtual Environment Setup
```powershell
cd d:\SELF\Internship\3SBC\vendor_email_prototype

# Create virtual environment
python -m venv .venv

# Activate venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration
Copy the example environment file:
```powershell
cp .env.example .env
```

Key variables in `.env`:
- `ENV`: Set to `development` (safe allowlist mode) or `production`.
- `EMAIL_PROVIDER`: Set to `mock` (zero network, deterministic testing) or `unitpost` (live API).
- `TEST_ALLOWLIST`: Comma-separated addresses allowed to receive mail in non-production.
- `UNITPOST_API_KEY`: Scoped Send & Contacts key from your Unitpost dashboard.
- `UNITPOST_WEBHOOK_SECRET`: `whsec_...` secret from Unitpost Webhook settings.
- `UNITPOST_FROM_EMAIL`: Verified sender address on your sending domain.

---

## 🚀 Running the Application

Start the FastAPI application:
```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

Open your browser to:
- **Web Dashboard**: `http://localhost:8001/`
- **Interactive OpenAPI Docs**: `http://localhost:8001/docs`

---

## 🌐 Exposing the Webhook via Tunnel

Unitpost requires a public HTTPS URL for webhooks.

### Using Cloudflare Tunnel (Quickest & Free)
```bash
cloudflared tunnel --url http://localhost:8001
```
Copy the generated URL (e.g., `https://xyz-random.trycloudflare.com`).

### Using ngrok
```bash
ngrok http 8001
```
Copy your forwarding URL (e.g., `https://abcdef.ngrok-free.app`).

### Configure Webhook in Unitpost Dashboard
1. Go to **Developers → Webhooks** in your Unitpost dashboard.
2. Click **Create Endpoint**.
3. Endpoint URL: `https://<YOUR-TUNNEL-URL>/webhooks/unitpost`.
4. Event Subscriptions:
   - `contact.updated`
   - `topic.unsubscribed`
   - `email.bounced`
   - `email.complained`
   - `email.suppressed`
5. Copy the signing secret (`whsec_...`) into your `.env` file as `UNITPOST_WEBHOOK_SECRET`.

---

## 🧪 Automated Testing

Run the full pytest suite (all 9 tests execute in memory via `MockProvider`):
```powershell
.\.venv\Scripts\pytest.exe -v tests
```

Tests verify:
- CSV cleaning (trimming, lowercase, invalid email formatting, duplicate handling).
- Suppressed addresses skipped on import.
- Suppressed addresses skipped on send and campaign batching.
- Unsubscribe webhooks creating permanent suppression records.
- Bounce and complaint webhooks triggering suppression.
- Idempotency (repeating the same webhook delivery is safely ignored).
- Bad HMAC signatures rejected with HTTP 400.
- Non-allowlisted sends refused in non-production.
- Admin re-subscription requiring mandatory legal justification and logging an audit record.

---

## 📋 Manual Test Checklist (Acceptance Criteria)

Follow these steps on `http://localhost:8001/`:

1. **Import Test Contacts**:
   - On the dashboard, locate **"1. Upload Vendor CSV"**.
   - Upload `sample_vendor_contacts.csv` (contains 10 test vendor contacts).
   - Verify the dashboard counter shows `Active Contacts: 10`.

2. **Send an Allowlisted Test**:
   - Under **"2. Send Allowlisted Test"**, enter:
     - Recipient: `tester@agency.example.com`
     - Company Name: `Talent Solutions Group`
   - Click **Dispatch Test Email**.
   - Verify success notification banner.
   - Try sending to `random_unregistered@vendor.com` $\rightarrow$ Verify refusal banner indicating recipient is not in `TEST_ALLOWLIST`.

3. **Simulate an Unsubscribe Webhook (Opt-out)**:
   - Fire a test webhook event simulating the recipient clicking "Unsubscribe" in their campaign email:
   ```powershell
   # In mock mode or live mode, the webhook handler suppresses the recipient
   curl -X POST http://localhost:8001/webhooks/unitpost `
     -H "Content-Type: application/json" `
     -H "svix-id: msg_manual_test_01" `
     -H "svix-timestamp: 1735689600" `
     -H "svix-signature: v1,..." # Or use the signed helper in tests
   ```
   *(Or trigger a Test Delivery from the Unitpost dashboard)*.
   - Refresh `http://localhost:8001/`:
     - Verify the address appears in the **Permanent Suppression Table**.
     - Verify the reason shows `unsubscribe`.

4. **Re-run the Campaign**:
   - Click **"TRIGGER BATCH SEND"**.
   - Check the campaign summary:
     - The unsubscribed vendor is **automatically excluded and skipped**.
     - The remaining eligible, allowlisted vendor is sent.
   - Re-running the campaign will report `All eligible recipients have already been sent` (demonstrating checkpointing idempotency).

5. **Test Admin Re-subscription**:
   - In the Suppression table, enter a reason next to an address (e.g., *"Signed 2026 addendum requesting reactivation"*).
   - Click **Re-subscribe**.
   - Address is removed from suppressions and logged in `suppression_audit`.

---

## 📝 Assumptions & Verifications

### Verified against Unitpost Documentation
1. **SDK**: Official package `unitpost` v0.6.0 on PyPI.
2. **Webhook Verification**: Svix-compatible headers `svix-id`, `svix-timestamp`, `svix-signature` using HMAC-SHA256 over `${svix-id}.${svix-timestamp}.${raw_body}` with `whsec_...` secret prefix base64url decoding.
3. **Campaign Flow**: Uses Unitpost campaigns (`POST /api/v1/email/campaigns` + `POST /api/v1/email/campaigns/{id}/send`) so hosted unsubscribe pages, List-Unsubscribe headers, and topic preferences automatically attach to outbound emails.
4. **Unsubscribe Events**: When a recipient clicks unsubscribe, Unitpost triggers `contact.updated` with `unsubscribed: true` or `topic.unsubscribed`.

### Assumptions Made
1. **Database**: Implemented using SQLite (`vendor_email.db`) with SQLAlchemy declarative models designed to be 100% PostgreSQL-compatible (string lengths, standard datetime timestamps, clean primary/foreign keys) for seamless migration.
2. **Footer Content**: Includes `CAN-SPAM` mandatory physical postal address and agency name configured via `AGENCY_NAME` and `AGENCY_POSTAL_ADDRESS` in `.env`.
