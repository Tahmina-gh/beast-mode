import secrets

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import notifications as N
from core import (ApiError, Form, current_user, login_required, login_user, make_token, ratelimit, read_token,
                  safe_next, save_upload)
from models import Member, Role, User, db

bp = Blueprint("auth", __name__)
HOME = {"member": "member.home", "trainer": "trainer.home", "admin": "admin.dashboard"}


def next_member_code():
    n = (db.session.query(db.func.max(Member.id)).scalar() or 0) + 1
    while Member.query.filter_by(member_code=f"BM-{n:06d}").first():
        n += 1
    return f"BM-{n:06d}"


def create_member_user(email, password, full_name, phone=None, verified=False, demo=False):
    role = Role.query.filter_by(name="member").first()
    u = User(email=email, password_hash=generate_password_hash(password), role_id=role.id, full_name=full_name,
             phone=phone, email_verified=verified, is_demo=demo)
    db.session.add(u)
    db.session.flush()
    db.session.add(Member(user_id=u.id, member_code=next_member_code(), qr_token=secrets.token_urlsafe(24)))
    db.session.commit()
    return u


def send_verification(user):
    link = current_app.config["BASE_URL"] + url_for("auth.verify", token=make_token("verify", user.id))
    N.send_email(user.email, "Verify your email", N.email_html("Confirm your email",
                 [f"Hi {user.first_name}, tap the button below to verify your email address. The link is valid for 48 hours."],
                 "Verify email", link))
    return link


def dev_hint(link):
    """Only in local development with no SMTP: show the link so you can test without an email server."""
    cfg = current_app.config
    if not cfg["IS_PROD"] and not cfg["SMTP_HOST"]:
        flash(f"Dev mode (no SMTP configured): verify here → {link}", "info")


@bp.route("/signup", methods=["GET", "POST"])
@ratelimit(8, 600)
def signup():
    if current_user():
        return redirect(url_for(HOME[current_user().role_name]))
    if request.method == "POST":
        f = Form(request.form)
        f.text("full_name", "Full name", max=100, min=2)
        f.email()
        f.phone(required=False)
        f.password()
        if request.form.get("password") != request.form.get("password2"):
            f.errors.setdefault("password2", "Passwords don't match.")
        if not request.form.get("terms"):
            f.errors.setdefault("terms", "Please accept the terms to continue.")
        if f.ok and User.query.filter_by(email=f.v["email"]).first():
            f.errors["email"] = "An account with that email already exists. Try logging in."
        if not f.ok:
            return render_template("auth/signup.html", errors=f.errors, form=request.form), 422
        u = create_member_user(f.v["email"], f.v["password"], f.v["full_name"], f.v["phone"] or None)
        link = send_verification(u)
        N.send_email(u.email, f"Welcome to {current_app.config['GYM_NAME']}", N.email_html(
            f"Welcome, {u.first_name}", ["Your account is ready. Pick a membership, book your first class and meet the coaches."],
            "Go to my dashboard", current_app.config["BASE_URL"] + "/member"))
        login_user(u)
        flash("Welcome! Your account is ready. We've sent you an email to verify your address.", "ok")
        dev_hint(link)
        return redirect(safe_next(request.args.get("next")) or url_for("member.home"))
    return render_template("auth/signup.html", errors={}, form={})


@bp.route("/login", methods=["GET", "POST"])
@ratelimit(10, 300)
def login():
    if current_user():
        return redirect(url_for(HOME[current_user().role_name]))
    if request.method == "POST":
        email, pw = (request.form.get("email") or "").strip().lower(), request.form.get("password") or ""
        u = User.query.filter_by(email=email).first()
        # constant-ish work + same message for unknown email / wrong password (no account enumeration)
        good = u and check_password_hash(u.password_hash, pw)
        if not good:
            return render_template("auth/login.html", error="Incorrect email or password.", form=request.form), 401
        if u.status != "active":
            return render_template("auth/login.html", error="This account is suspended. Please contact the gym.", form=request.form), 403
        login_user(u)
        return redirect(safe_next(request.args.get("next") or request.form.get("next")) or url_for(HOME[u.role_name]))
    return render_template("auth/login.html", error=None, form={})


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You've been logged out.", "ok")
    return redirect(url_for("site.index"))


@bp.route("/verify/<token>")
def verify(token):
    uid = read_token("verify", token, 60 * 60 * 48)
    u = db.session.get(User, uid) if uid else None
    if not u:
        flash("That verification link is invalid or has expired. Request a new one from your account page.", "error")
        return redirect(url_for("auth.login"))
    u.email_verified = True
    db.session.commit()
    flash("Email verified — thank you!", "ok")
    return redirect(url_for(HOME[u.role_name]) if current_user() else url_for("auth.login"))


@bp.route("/verify/resend", methods=["POST"])
@login_required
@ratelimit(3, 600)
def resend_verification():
    u = current_user()
    if u.email_verified:
        flash("Your email is already verified.", "ok")
    else:
        link = send_verification(u)
        flash("Verification email sent. Check your inbox.", "ok")
        dev_hint(link)
    return redirect(request.referrer or url_for("account.profile"))


@bp.route("/forgot", methods=["GET", "POST"])
@ratelimit(5, 600)
def forgot():
    sent = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        u = User.query.filter_by(email=email).first()
        if u and u.status == "active":
            # token is bound to the current password hash => single use, dies after reset
            tok = make_token("reset", [u.id, u.password_hash[-12:]])
            link = current_app.config["BASE_URL"] + url_for("auth.reset", token=tok)
            N.send_email(u.email, "Reset your password", N.email_html("Reset your password",
                         ["We received a request to reset your password. This link works once and expires in 1 hour.",
                          "If you didn't ask for this, you can ignore this email."], "Choose a new password", link))
            dev_hint(link)
        sent = True  # identical response either way
    return render_template("auth/forgot.html", sent=sent)


@bp.route("/reset/<token>", methods=["GET", "POST"])
@ratelimit(10, 600)
def reset(token):
    data = read_token("reset", token, 3600)
    u = db.session.get(User, data[0]) if data else None
    if not u or u.password_hash[-12:] != data[1]:
        flash("That reset link is invalid or has already been used. Request a new one.", "error")
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        f = Form(request.form)
        f.password()
        if request.form.get("password") != request.form.get("password2"):
            f.errors.setdefault("password2", "Passwords don't match.")
        if not f.ok:
            return render_template("auth/reset.html", errors=f.errors), 422
        u.password_hash = generate_password_hash(f.v["password"])
        db.session.commit()
        flash("Password updated. You can log in now.", "ok")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", errors={})


# --------------------------------------------------------------- account page
account = Blueprint("account", __name__, url_prefix="/account")


@account.route("/", methods=["GET", "POST"])
@login_required
def profile():
    u = current_user()
    errors = {}
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            f = Form(request.form)
            f.text("full_name", "Full name", max=100, min=2)
            f.phone(required=False)
            if u.member:
                f.date("dob", "Date of birth", required=False, past=True)
                f.text("emergency_contact", "Emergency contact", required=False, max=160)
            if f.ok:
                u.full_name, u.phone = f.v["full_name"], f.v["phone"] or None
                if u.member:
                    u.member.dob, u.member.emergency_contact = f.v["dob"], f.v["emergency_contact"] or None
                photo = request.files.get("photo")
                if photo and photo.filename:
                    u.photo = save_upload(photo, "image", u)
                db.session.commit()
                flash("Profile updated.", "ok")
                return redirect(url_for("account.profile"))
            errors = f.errors
        elif action == "password":
            f = Form(request.form)
            f.password("new_password")
            if not check_password_hash(u.password_hash, request.form.get("current_password") or ""):
                f.errors["current_password"] = "Current password is incorrect."
            if request.form.get("new_password") != request.form.get("new_password2"):
                f.errors.setdefault("new_password2", "Passwords don't match.")
            if f.ok:
                u.password_hash = generate_password_hash(f.v["new_password"])
                db.session.commit()
                flash("Password changed.", "ok")
                return redirect(url_for("account.profile"))
            errors = f.errors
    return render_template("auth/account.html", errors=errors), (422 if errors else 200)


@account.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    from models import Notification
    u = current_user()
    if request.method == "POST":
        Notification.query.filter_by(user_id=u.id, read_at=None).update({"read_at": db.func.now()})
        db.session.commit()
        return redirect(url_for("account.notifications"))
    items = Notification.query.filter_by(user_id=u.id).order_by(Notification.created_at.desc()).limit(60).all()
    return render_template("auth/notifications.html", items=items)
