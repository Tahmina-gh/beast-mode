import os
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")
    IS_PROD = APP_ENV == "production"

    # Secrets come ONLY from the environment. Production refuses to boot without one.
    SECRET_KEY = os.environ.get("AUTH_SECRET") or (None if IS_PROD else "dev-only-insecure-secret")

    _db = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(BASE_DIR, "instance", "beast.db")
    if _db.startswith("sqlite:///") and not _db.startswith("sqlite:////"):
        _db = "sqlite:///" + os.path.join(BASE_DIR, _db[len("sqlite:///"):])
    SQLALCHEMY_DATABASE_URI = _db.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    GYM_NAME = os.environ.get("GYM_NAME", "BEAST MODE")
    BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    CURRENCY = os.environ.get("CURRENCY", "usd").lower()
    REQUIRE_VERIFIED_EMAIL = _bool("REQUIRE_VERIFIED_EMAIL", "1")
    CANCEL_CUTOFF_HOURS = int(os.environ.get("CANCEL_CUTOFF_HOURS", "2"))
    WAITLIST_OFFER_HOURS = int(os.environ.get("WAITLIST_OFFER_HOURS", "2"))
    CHECKIN_COOLDOWN_MIN = int(os.environ.get("CHECKIN_COOLDOWN_MIN", "10"))

    # Sessions / cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("COOKIE_SECURE", "1" if IS_PROD else "0")
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 14
    TRUST_PROXY = _bool("TRUST_PROXY")
    RATELIMIT_ENABLED = True

    # Uploads
    UPLOAD_DIR = os.path.abspath(os.path.join(BASE_DIR, os.environ.get("UPLOAD_DIR", "uploads")))
    MAX_CONTENT_LENGTH = 110 * 1024 * 1024
    MAX_IMAGE_MB = 8
    MAX_VIDEO_MB = 100

    # Payments (Stripe Checkout; blank => front-desk/manual mode)
    STRIPE_SECRET_KEY = os.environ.get("PAYMENT_SECRET", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # Email (SMTP)
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("EMAIL_API_KEY", "")
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "BEAST MODE <no-reply@localhost>")
    ADMIN_NOTIFY_EMAIL = os.environ.get("ADMIN_NOTIFY_EMAIL", "")

    # Optional SMS / WhatsApp
    TWILIO_SID = os.environ.get("TWILIO_SID", "")
    TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
    TWILIO_FROM = os.environ.get("TWILIO_FROM", "")
