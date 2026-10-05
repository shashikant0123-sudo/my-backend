from fastapi import FastAPI, APIRouter, HTTPException, Header
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import re
import uuid
import logging
import ipaddress
import httpx
from pathlib import Path
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

EMAIL_BASE_URL = "https://integrations.emergentagent.com"
EMAIL_KEY = os.environ.get("EMERGENT_EMAIL_KEY")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "OM Ayodhya Travels")
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL")
ADMIN_KEY = os.environ.get("ADMIN_KEY")

_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "goo.gl", "rebrand.ly")
_CRED_ASK = ("reply with your password", "reply with the code", "send your password", "cvv",
             "send us your password", "enter your password below", "confirm your card number",
             "your full card number", "seed phrase", "recovery phrase", "verify your card",
             "social security number", "confirm your bank details")
_HOSTISH = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)


def _host_ok(host: str) -> bool:
    if not host or "xn--" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not any(host == s or host.endswith("." + s) for s in _SHORTENERS)


def _same_site(shown: str, real: str) -> bool:
    return shown == real or real.endswith("." + shown) or shown.endswith("." + real)


class _EmailScan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.urls, self.anchors = set(), [], []
        self._href, self._text = None, []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag.lower())
        self.urls += [v for k, v in attrs if k.lower() in ("href", "src") and v]
        if tag.lower() == "a":
            self._href = dict((k.lower(), v) for k, v in attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, "".join(self._text)))
            self._href, self._text = None, []


def _assert_safe_email(subject: str, html: str) -> None:
    scan = _EmailScan()
    scan.feed(html)
    if scan.tags & {"form", "input", "textarea", "select"}:
        raise ValueError("No forms or input fields in email (G2)")
    body = f"{subject}\n{html}".lower()
    for p in _CRED_ASK:
        if p in body:
            raise ValueError(f"Email asks the recipient for credentials: {p!r} (G2)")
    for url in scan.urls:
        low = url.strip().lower()
        if low.startswith(("mailto:", "tel:", "cid:", "#")):
            continue
        if not low.startswith("https://"):
            raise ValueError(f"Email links/assets must be absolute https: {url!r} (G3)")
        host = urlparse(low).hostname or ""
        if not _host_ok(host) or urlparse(low).username is not None:
            raise ValueError(f"Shortened, numeric-host or credential-bearing URL: {url!r} (G3)")
    for href, text in scan.anchors:
        real = urlparse(href.strip().lower()).hostname or ""
        if not real:
            continue
        for m in _HOSTISH.finditer(text):
            if not _same_site(m.group(1).lower(), real):
                raise ValueError(f"Anchor text {m.group(1)!r} != real link host {real!r} (G3)")


async def send_email(*, to: str, subject: str, html: str, reply_to: Optional[str] = None) -> Optional[str]:
    _assert_safe_email(subject, html)
    payload = {"to": [to], "subject": subject, "html": html, "from_name": EMAIL_FROM_NAME}
    if reply_to or EMAIL_REPLY_TO:
        payload["contact_email"] = reply_to or EMAIL_REPLY_TO
    async with httpx.AsyncClient(timeout=30) as client_http:
        resp = await client_http.post(
            f"{EMAIL_BASE_URL}/api/v1/email/send",
            headers={"X-Email-Key": EMAIL_KEY},
            json=payload,
        )
    resp.raise_for_status()
    return resp.json().get("id")


class EnquiryCreate(BaseModel):
    name: str
    mobile: str
    whatsapp: Optional[str] = None
    email: Optional[str] = None
    pickup: Optional[str] = None
    destination: Optional[str] = None
    travel_date: Optional[str] = None
    return_date: Optional[str] = None
    passengers: Optional[str] = None
    vehicle: Optional[str] = None
    trip_type: Optional[str] = None
    days: Optional[str] = None
    message: Optional[str] = None
    source_page: Optional[str] = None


ENQUIRY_FIELDS = [
    ("Name", "name"), ("Mobile", "mobile"), ("Email", "email"),
    ("Pickup Location", "pickup"), ("Destination", "destination"), ("Travel Date", "travel_date"),
    ("Return Date", "return_date"), ("Passengers", "passengers"), ("Vehicle", "vehicle"),
    ("Days", "days"), ("Message", "message"), ("Source Page", "source_page"),
]


async def notify_owner(doc: dict) -> None:
    if not (EMAIL_KEY and OWNER_EMAIL):
        logger.info("Email not configured; enquiry stored only.")
        return
    rows = "".join(
        f'<tr><td style="padding:8px 12px;border:1px solid #e5e0d5;font-weight:600;color:#0F2F24">{label}</td>'
        f'<td style="padding:8px 12px;border:1px solid #e5e0d5;color:#3D4A41">{escape(str(doc.get(key) or "—"))}</td></tr>'
        for label, key in ENQUIRY_FIELDS
    )
    subject = f"New Tour Enquiry — {escape(str(doc.get('name') or 'Website Visitor'))}"
    html = (
        '<table role="presentation" width="100%"><tr><td style="padding:24px;font-family:Arial,sans-serif">'
        f'<h2 style="color:#0F2F24;margin:0 0 16px">New Tour Enquiry — {escape(EMAIL_FROM_NAME)}</h2>'
        f'<table role="presentation" style="border-collapse:collapse;font-size:14px">{rows}</table>'
        f'<p style="font-size:12px;color:#888;margin-top:20px">Sent by the {escape(EMAIL_FROM_NAME)} website enquiry system. '
        'We never ask for your password or card details by email.</p></td></tr></table>'
    )
    try:
        await send_email(to=OWNER_EMAIL, subject=subject, html=html)
    except Exception as e:
        logger.error(f"Enquiry email failed: {e}")


@api_router.get("/")
async def root():
    return {"message": "OM Ayodhya Travels API"}


@api_router.get("/health")
async def health():
    return {"status": "ok"}


@api_router.post("/enquiries")
async def create_enquiry(input: EnquiryCreate):
    doc = input.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["status"] = "new"
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.enquiries.insert_one(doc)
    doc.pop("_id", None)
    await notify_owner(doc)
    return {"status": "success", "id": doc["id"],
            "message": "Thank you! Your enquiry has been received. Our travel team will call you back shortly with a free quote."}


@api_router.get("/enquiries")
async def list_enquiries(x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await db.enquiries.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)


class ReviewCreate(BaseModel):
    name: str
    tour: Optional[str] = "General"
    rating: int = 5
    text: str


@api_router.get("/reviews")
async def list_reviews():
    return await db.reviews.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)


@api_router.post("/admin/reviews")
async def create_review(input: ReviewCreate, x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    doc = input.model_dump()
    doc["rating"] = max(1, min(5, int(doc["rating"])))
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.reviews.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.delete("/admin/reviews/{review_id}")
async def delete_review(review_id: str, x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    res = await db.reviews.delete_one({"id": review_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Review not found")
    return {"status": "deleted"}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or uuid.uuid4().hex[:8]


class BlogPostCreate(BaseModel):
    title: str
    excerpt: Optional[str] = ""
    content: str
    cover_image: Optional[str] = ""
    related_path: Optional[str] = ""
    related_label: Optional[str] = ""


@api_router.get("/blog")
async def list_blog_posts():
    return await db.blog_posts.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)


@api_router.get("/blog/{slug}")
async def get_blog_post(slug: str):
    doc = await db.blog_posts.find_one({"slug": slug}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Post not found")
    return doc


@api_router.post("/admin/blog")
async def create_blog_post(input: BlogPostCreate, x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    doc = input.model_dump()
    doc["id"] = str(uuid.uuid4())
    base = _slugify(doc["title"])
    slug, n = base, 2
    while await db.blog_posts.find_one({"slug": slug}):
        slug = f"{base}-{n}"
        n += 1
    doc["slug"] = slug
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.blog_posts.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.put("/admin/blog/{post_id}")
async def update_blog_post(post_id: str, input: BlogPostCreate, x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    res = await db.blog_posts.update_one({"id": post_id}, {"$set": input.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Post not found")
    return await db.blog_posts.find_one({"id": post_id}, {"_id": 0})


@api_router.delete("/admin/blog/{post_id}")
async def delete_blog_post(post_id: str, x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    res = await db.blog_posts.delete_one({"id": post_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"status": "deleted"}


IST = ZoneInfo("Asia/Kolkata")


def _tomorrow_ist() -> str:
    return (datetime.now(IST) + timedelta(days=1)).date().isoformat()


async def run_digest() -> dict:
    """Email the owner tomorrow's upcoming tours. Raises if the email send fails."""
    if not (EMAIL_KEY and OWNER_EMAIL):
        return {"status": "skipped", "count": 0, "message": "Email not configured"}
    tomorrow = _tomorrow_ist()
    upcoming = await db.enquiries.find(
        {"status": "upcoming", "travel_date": tomorrow}, {"_id": 0}
    ).sort("created_at", 1).to_list(200)
    if not upcoming:
        return {"status": "skipped", "count": 0, "message": f"No tours scheduled for {tomorrow}"}
    header_cells = "".join(
        f'<th style="padding:8px 12px;border:1px solid #d8e2ec;text-align:left;background:#0F4C81;color:#ffffff">{h}</th>'
        for h in ["Guest", "Mobile", "Destination", "Travel Date", "Vehicle", "Pax"]
    )
    body_rows = "".join(
        "<tr>" + "".join(
            f'<td style="padding:8px 12px;border:1px solid #e5e0d5;color:#3D4A41">{escape(str(v))}</td>'
            for v in [e.get("name") or "—", e.get("mobile") or "—", e.get("destination") or "—",
                      e.get("travel_date") or "—", e.get("vehicle") or "—", e.get("passengers") or "—"]
        ) + "</tr>"
        for e in upcoming
    )
    subject = f"Tomorrow's Tours — {len(upcoming)} booking(s) on {tomorrow}"
    html = (
        '<table role="presentation" width="100%"><tr><td style="padding:24px;font-family:Arial,sans-serif">'
        f'<h2 style="color:#0F4C81;margin:0 0 6px">Tomorrow&apos;s Tours — {tomorrow}</h2>'
        f'<p style="color:#64748B;font-size:13px;margin:0 0 16px">{len(upcoming)} tour(s) departing tomorrow — {escape(EMAIL_FROM_NAME)}</p>'
        f'<table role="presentation" style="border-collapse:collapse;font-size:14px;width:100%"><thead><tr>{header_cells}</tr></thead><tbody>{body_rows}</tbody></table>'
        f'<p style="font-size:12px;color:#888;margin-top:20px">Daily 3 PM digest from the {escape(EMAIL_FROM_NAME)} booking dashboard. '
        'We never ask for your password or card details by email.</p></td></tr></table>'
    )
    await send_email(to=OWNER_EMAIL, subject=subject, html=html)
    return {"status": "sent", "count": len(upcoming), "date": tomorrow}


@api_router.post("/admin/digest")
async def send_upcoming_digest(x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        return await run_digest()
    except Exception as e:
        logger.error(f"Digest email failed: {e}")
        raise HTTPException(status_code=502, detail="Email send failed")


async def _scheduled_digest():
    try:
        logger.info(f"Scheduled daily digest: {await run_digest()}")
    except Exception as e:
        logger.error(f"Scheduled digest failed: {e}")


scheduler = AsyncIOScheduler(timezone=IST)


VALID_STATUSES = {"new", "upcoming", "completed", "cancelled", "failed"}


class StatusUpdate(BaseModel):
    status: str


class PriceRow(BaseModel):
    vehicle: str
    price: str


class PricingUpdate(BaseModel):
    prices: list[PriceRow]


@api_router.patch("/enquiries/{enquiry_id}/status")
async def update_enquiry_status(enquiry_id: str, body: StatusUpdate, x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    res = await db.enquiries.update_one({"id": enquiry_id}, {"$set": {"status": body.status}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    return {"status": "updated"}


@api_router.get("/prices")
async def get_prices():
    docs = await db.tour_prices.find({}, {"_id": 0}).to_list(200)
    return {d["slug"]: d["prices"] for d in docs}


@api_router.put("/admin/prices/{slug}")
async def upsert_prices(slug: str, body: PricingUpdate, x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    prices = [p.model_dump() for p in body.prices if p.vehicle.strip() and p.price.strip()]
    await db.tour_prices.update_one({"slug": slug}, {"$set": {"slug": slug, "prices": prices}}, upsert=True)
    return {"status": "saved", "slug": slug, "count": len(prices)}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def start_daily_digest_scheduler():
    scheduler.add_job(
        _scheduled_digest,
        CronTrigger(hour=15, minute=0, timezone=IST),
        id="daily_upcoming_digest",
        replace_existing=True,
    )
    scheduler.start()


@app.on_event("shutdown")
async def shutdown_db_client():
    scheduler.shutdown(wait=False)
    client.close()
