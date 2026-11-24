from __future__ import annotations
import datetime
import os
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Request, Response, status, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db, init_db, Contact, Suppression, SuppressionAudit, CampaignCheckpoint, CampaignState
from app.email_provider import get_email_provider
from app.csv_processor import process_contacts_csv
from app.campaign_engine import send_test_email, dispatch_campaign_batch
from app.webhook_handler import handle_unitpost_webhook

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title=f"{settings.AGENCY_NAME} — Vendor Email Module",
    description="Compliance-first outreach module with Unitpost campaigns and permanent suppression guard.",
    version="1.0.0",
    lifespan=lifespan,
)

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ==========================================
# 1. Web Dashboard
# ==========================================
@app.get("/", response_class=HTMLResponse)
def get_dashboard(request: Request, message: Optional[str] = None, db: Session = Depends(get_db)):
    provider = get_email_provider()
    total_contacts = db.query(Contact).count()
    contacts = db.query(Contact).order_by(Contact.imported_at.desc()).all()
    suppressions = db.query(Suppression).order_by(Suppression.created_at.desc()).all()
    suppressed_emails = {s.email for s in suppressions}
    allowlist = set(settings.TEST_ALLOWLIST) if isinstance(settings.TEST_ALLOWLIST, list) else set()

    # Daily send calculation
    today_start = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = db.query(CampaignCheckpoint).filter(
        CampaignCheckpoint.sent_at >= today_start,
        CampaignCheckpoint.status == "sent"
    ).count()

    campaign_state = db.query(CampaignState).filter(CampaignState.campaign_id == "vendor-blast-01").first()
    campaign_status = campaign_state.status if campaign_state else "ready"
    total_sent_campaign = campaign_state.sent_count if campaign_state else 0

    stats = provider.get_stats("vendor-blast-01")
    # Supplement stats with local suppression numbers if provider has none yet
    if stats.get("unsubscribed", 0) == 0:
        stats["unsubscribed"] = sum(1 for s in suppressions if s.reason == "unsubscribe")
    if stats.get("bounced", 0) == 0:
        stats["bounced"] = sum(1 for s in suppressions if s.reason == "bounce")
    if stats.get("complained", 0) == 0:
        stats["complained"] = sum(1 for s in suppressions if s.reason == "complaint")

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "agency_name": settings.AGENCY_NAME,
            "env": settings.ENV,
            "provider": settings.EMAIL_PROVIDER,
            "message": message,
            "total_contacts": total_contacts,
            "contacts": contacts,
            "suppressed_emails": suppressed_emails,
            "allowlist": allowlist,
            "sent_today": sent_today,
            "daily_limit": settings.DAILY_LIMIT,
            "campaign_status": campaign_status,
            "total_sent_campaign": total_sent_campaign,
            "stats": stats,
            "suppressions": suppressions,
        }
    )


# ==========================================
# 2. Contact CSV Import & Download
# ==========================================
@app.get("/download/test-contacts")
def download_test_contacts():
    csv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_contacts.csv")
    if os.path.exists(csv_path):
        return FileResponse(path=csv_path, filename="test_contacts.csv", media_type="text/csv")
    raise HTTPException(status_code=404, detail="File not found")

@app.post("/contacts/import")
async def import_contacts_endpoint(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    provider = get_email_provider()
    content = await file.read()
    try:
        csv_text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        csv_text = content.decode("latin-1")

    try:
        result = process_contacts_csv(csv_text=csv_text, db=db, provider=provider)
    except Exception as exc:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"/?message=Import+Error:+{exc}", status_code=status.HTTP_303_SEE_OTHER)
        raise HTTPException(status_code=400, detail=str(exc))

    msg = f"Imported: {result['imported']}, Duplicates: {result['duplicates']}, Invalid: {result['invalid']}, Suppressed: {result['suppressed']}"
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/?message={msg}", status_code=status.HTTP_303_SEE_OTHER)
    return result


# ==========================================
# 3. Test Email Send
# ==========================================
@app.post("/campaign/test")
def send_test_endpoint(
    request: Request,
    to_email: str = Form(...),
    company_name: str = Form("Valued Partner"),
    db: Session = Depends(get_db)
):
    provider = get_email_provider()
    try:
        result = send_test_email(to_email=to_email, company_name=company_name, db=db, provider=provider)
    except PermissionError as exc:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"/?message=Permission+Denied:+{exc}", status_code=status.HTTP_303_SEE_OTHER)
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"/?message=Error:+{exc}", status_code=status.HTTP_303_SEE_OTHER)
        raise HTTPException(status_code=400, detail=str(exc))

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/?message=Test+email+dispatched+successfully+to+{to_email}", status_code=status.HTTP_303_SEE_OTHER)
    return result


# ==========================================
# 4. Campaign Batch Dispatch & Pause
# ==========================================
@app.post("/campaign/send")
def send_campaign_endpoint(
    request: Request,
    campaign_id: str = Form("vendor-blast-01"),
    confirm: bool = Form(False),
    db: Session = Depends(get_db)
):
    provider = get_email_provider()
    result = dispatch_campaign_batch(
        campaign_id=campaign_id,
        confirm=confirm,
        db=db,
        provider=provider
    )
    if "text/html" in request.headers.get("accept", ""):
        msg = result.get("message") or f"Dispatched {result.get('sent_in_this_batch', 0)} messages."
        return RedirectResponse(f"/?message={msg}", status_code=status.HTTP_303_SEE_OTHER)
    return result


@app.post("/campaign/pause")
def pause_campaign_endpoint(
    request: Request,
    campaign_id: str = Form("vendor-blast-01"),
    db: Session = Depends(get_db)
):
    provider = get_email_provider()
    provider.pause_campaign(campaign_id)

    state = db.query(CampaignState).filter(CampaignState.campaign_id == campaign_id).first()
    if state:
        state.status = "paused"
        db.commit()

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/?message=Campaign+paused+cleanly.", status_code=status.HTTP_303_SEE_OTHER)
    return {"status": "paused", "campaign_id": campaign_id}


# ==========================================
# 5. Admin Suppression & Re-subscribe
# ==========================================
@app.get("/suppressions")
def list_suppressions_endpoint(db: Session = Depends(get_db)):
    suppressions = db.query(Suppression).order_by(Suppression.created_at.desc()).all()
    return [
        {
            "email": s.email,
            "reason": s.reason,
            "source": s.source,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in suppressions
    ]


@app.post("/admin/resubscribe")
def resubscribe_contact_endpoint(
    request: Request,
    email: str = Form(...),
    reason: Optional[str] = Form(None),
    admin_user: str = Form("admin"),
    db: Session = Depends(get_db)
):
    clean_email = email.strip().lower()
    clean_reason = (reason or "").strip()

    if not clean_reason:
        raise HTTPException(status_code=400, detail="A mandatory justification reason is required to re-subscribe an address.")

    suppression = db.query(Suppression).filter(Suppression.email == clean_email).first()
    if not suppression:
        raise HTTPException(status_code=404, detail=f"Address '{clean_email}' is not in the suppression list.")

    # Record mandatory audit entry before removal
    audit_entry = SuppressionAudit(
        email=clean_email,
        action="unsuppressed",
        reason=clean_reason,
        admin_user=admin_user,
    )
    db.add(audit_entry)
    db.delete(suppression)
    db.commit()

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/?message=Address+{clean_email}+re-subscribed+and+logged+in+audit+table.", status_code=status.HTTP_303_SEE_OTHER)
    return {"status": "unsuppressed", "email": clean_email, "audit_logged": True}


# ==========================================
# 6. Webhooks (Unitpost HMAC Signature Verified)
# ==========================================
@app.post("/webhooks/unitpost")
async def unitpost_webhook_endpoint(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    headers_dict = dict(request.headers)

    try:
        result = handle_unitpost_webhook(
            raw_body=raw_body,
            headers=headers_dict,
            db=db,
        )
        return JSONResponse(status_code=200, content=result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal webhook error: {exc}")
