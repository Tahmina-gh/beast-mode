import hmac
import os
import re
import secrets
import time as _time
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps

from flask import abort, current_app, flash, g, jsonify, redirect, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from PIL import Image

from models import User, Media, db


class ApiError(Exception):
    def __init__(self, message, status=400, code=None, fields=None):
        super().__init__(message)
        self.message, self.status, self.code, self.fields = message, status, code, fields


def wants_json():
    return request.path.startswith("/api/") or request.is_json or request.headers.get("X-Requested-With") == "fetch"


def ok(**kw):
    return jsonify(ok=True, **kw)


# ------------------------------------------------------------------ sessions
def current_user():
    if "_cu" in g:
        return g._cu
    user = None
    uid = session.get("uid")
    if uid:
        user = db.session.get(User, uid)
        if user and user.status != "active":
            user = None
            session.pop("uid", None)
    g._cu = user
    return user


def login_user(user):
    session.clear()  # prevents session fixation
    session["uid"] = user.id
    session["_csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    user.last_login = datetime.now()
    db.session.commit()


def login_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            u = current_user()
            if not u:
                if wants_json():
                    raise ApiError("Please log in to continue.", 401, "auth_required")
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            if roles and u.role_name not in roles:
                if wants_json():
                    raise ApiError("You don't have permission to do that.", 403)
                abort(403)
            return f(*a, **k)
        return wrapper
    if len(roles) == 1 and callable(roles[0]):
        f, roles = roles[0], ()
        return deco(f)
    return deco


def require_member():
    u = current_user()
    if not u or not u.member:
        raise ApiError("Member account required.", 403)
    return u.member


def require_verified(user):
    if current_app.config["REQUIRE_VERIFIED_EMAIL"] and not user.email_verified:
        raise ApiError("Please verify your email address first — check your inbox for the link.", 403, "email_unverified")


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# ---------------------------------------------------------------------- CSRF
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


CSRF_EXEMPT = {"api.stripe_webhook"}


def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.endpoint not in CSRF_EXEMPT:
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            raise ApiError("Your session expired for security. Please reload the page and try again.", 403, "csrf")


# ------------------------------------------------------------- rate limiting
_hits = defaultdict(deque)


def ratelimit(limit, window, scope=None):
    """In-memory sliding window per IP. Use Redis (or a proxy limiter) when running multiple workers."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if current_app.config.get("RATELIMIT_ENABLED", True) and request.method in ("POST", "PUT", "PATCH", "DELETE"):
                key = (scope or request.endpoint, request.remote_addr or "?")
                t = _time.time()
                q = _hits[key]
                while q and q[0] < t - window:
                    q.popleft()
                if len(q) >= limit:
                    raise ApiError("Too many attempts. Please wait a minute and try again.", 429, "rate_limited")
                q.append(t)
            return f(*a, **k)
        return wrapper
    return deco


# ---------------------------------------------------------------- validation
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{6,18}$")


class Form:
    """Server-side validation. Collects errors per field and raises one clear message."""

    def __init__(self, data):
        self.d, self.errors, self.v = data, {}, {}

    def _raw(self, k):
        x = self.d.get(k)
        return x.strip() if isinstance(x, str) else x

    def _err(self, k, msg):
        self.errors.setdefault(k, msg)

    def text(self, k, label=None, required=True, max=200, min=1):
        label, x = label or k.replace("_", " ").title(), self._raw(k) or ""
        if not x:
            if required:
                self._err(k, f"{label} is required.")
        elif len(x) > max or len(x) < min:
            self._err(k, f"{label} must be {min}-{max} characters.")
        self.v[k] = x
        return x

    def email(self, k="email", required=True):
        x = (self._raw(k) or "").lower()
        if not x and required:
            self._err(k, "Email is required.")
        elif x and (not EMAIL_RE.match(x) or len(x) > 190):
            self._err(k, "Please enter a valid email address.")
        self.v[k] = x
        return x

    def phone(self, k="phone", required=True):
        x = self._raw(k) or ""
        if not x and required:
            self._err(k, "Phone number is required.")
        elif x and not PHONE_RE.match(x):
            self._err(k, "Please enter a valid phone number.")
        self.v[k] = x
        return x

    def password(self, k="password"):
        x = self.d.get(k) or ""
        if len(x) < 8 or not re.search(r"[A-Za-z]", x) or not re.search(r"\d", x) or len(x) > 128:
            self._err(k, "Password must be at least 8 characters and include a letter and a number.")
        self.v[k] = x
        return x

    def date(self, k, label=None, required=True, future=False, past=False):
        label, x = label or k.replace("_", " ").title(), self._raw(k)
        val = None
        if not x:
            if required:
                self._err(k, f"{label} is required.")
        else:
            try:
                val = datetime.strptime(x, "%Y-%m-%d").date()
                if future and val < datetime.now().date():
                    self._err(k, f"{label} can't be in the past.")
                if past and val > datetime.now().date():
                    self._err(k, f"{label} can't be in the future.")
            except ValueError:
                self._err(k, f"{label} is not a valid date.")
        self.v[k] = val
        return val

    def int_(self, k, label=None, required=True, min=None, max=None):
        label, x = label or k.replace("_", " ").title(), self._raw(k)
        val = None
        if x in (None, ""):
            if required:
                self._err(k, f"{label} is required.")
        else:
            try:
                val = int(x)
                if (min is not None and val < min) or (max is not None and val > max):
                    self._err(k, f"{label} must be between {min} and {max}.")
            except (ValueError, TypeError):
                self._err(k, f"{label} must be a whole number.")
        self.v[k] = val
        return val

    def float_(self, k, label=None, required=True, min=None, max=None):
        label, x = label or k.replace("_", " ").title(), self._raw(k)
        val = None
        if x in (None, ""):
            if required:
                self._err(k, f"{label} is required.")
        else:
            try:
                val = float(x)
                if (min is not None and val < min) or (max is not None and val > max):
                    self._err(k, f"{label} must be between {min} and {max}.")
            except (ValueError, TypeError):
                self._err(k, f"{label} must be a number.")
        self.v[k] = val
        return val

    def choice(self, k, options, label=None, required=True):
        label, x = label or k.replace("_", " ").title(), self._raw(k) or ""
        if not x and not required:
            pass
        elif x not in options:
            self._err(k, f"Please choose a valid {label.lower()}.")
        self.v[k] = x
        return x

    @property
    def ok(self):
        return not self.errors

    def check(self):
        if self.errors:
            raise ApiError(next(iter(self.errors.values())), 422, "validation", self.errors)


def payload():
    """JSON body or form body as a dict."""
    return request.get_json(silent=True) or request.form.to_dict()


# ---------------------------------------------------------------- tokens
def _ser(purpose):
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=purpose)


def make_token(purpose, data):
    return _ser(purpose).dumps(data)


def read_token(purpose, token, max_age):
    try:
        return _ser(purpose).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


# ---------------------------------------------------------------- uploads
IMG_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
VID_EXT = {"mp4", "webm"}


def save_upload(file, kind="image", uploader=None, register=True):
    """Validated upload. Extension allow-list + real content check + random filename. Returns relative path."""
    if not file or not file.filename:
        return None
    cfg = current_app.config
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    allowed = IMG_EXT if kind == "image" else IMG_EXT | VID_EXT
    if ext not in allowed:
        raise ApiError(f"Unsupported file type. Allowed: {', '.join(sorted(allowed))}.", 422)
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    is_video = ext in VID_EXT
    if size > (cfg["MAX_VIDEO_MB"] if is_video else cfg["MAX_IMAGE_MB"]) * 1024 * 1024:
        raise ApiError(f"File too large (max {cfg['MAX_VIDEO_MB'] if is_video else cfg['MAX_IMAGE_MB']} MB).", 422)
    head = file.stream.read(16)
    file.stream.seek(0)
    if is_video:
        if not ((ext == "mp4" and head[4:8] == b"ftyp") or (ext == "webm" and head[:4] == b"\x1a\x45\xdf\xa3")):
            raise ApiError("That file doesn't look like a valid video.", 422)
    else:
        try:
            Image.open(file.stream).verify()
        except Exception:
            raise ApiError("That file doesn't look like a valid image.", 422)
        file.stream.seek(0)
    sub = datetime.now().strftime("%Y%m")
    name = f"{secrets.token_hex(12)}.{ext}"
    folder = os.path.join(cfg["UPLOAD_DIR"], sub)
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, name))
    rel = f"{sub}/{name}"
    if register:
        db.session.add(Media(filename=rel, original_name=(file.filename or "")[:200],
                             kind="video" if is_video else "image", size=size,
                             uploaded_by=uploader.id if uploader else None))
    return rel


def media_url(path):
    if not path:
        return ""
    return path if path.startswith(("http://", "https://", "/")) else f"/uploads/{path}"
