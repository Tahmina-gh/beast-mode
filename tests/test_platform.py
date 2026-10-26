from datetime import date, datetime, timedelta

import pytest

from conftest import api_post, csrf_meta, login, logout, signup
from models import (ClassBooking, GymClass, Member, Membership, MembershipPlan, Payment, Trainer,
                    TrainerAvailability, TrainerSession, User, db)
import services as S
from seed import create_admin, seed_demo


def make_member(app, email, verified=True, with_plan=True):
    from routes_auth import create_member_user
    u = create_member_user(email, "Passw0rd1", email.split("@")[0].title(), verified=verified)
    if with_plan:
        plan = MembershipPlan.query.filter_by(name="Performance").first()
        m, pay = S.create_membership_purchase(u.member, plan, "monthly")
        S.fulfill_payment(pay, "TEST")
    return u


def make_class(capacity=2, hours=24):
    t = Trainer.query.first()
    c = GymClass(name="Test Class", category="Strength", trainer_id=t.id if t else None,
                 starts_at=datetime.now() + timedelta(hours=hours), ends_at=datetime.now() + timedelta(hours=hours, minutes=45),
                 capacity=capacity)
    db.session.add(c)
    db.session.commit()
    return c


# ----------------------------------------------------------------- auth
def test_signup_login_logout(client):
    r = signup(client)
    assert r.status_code == 302 and "/member" in r.headers["Location"]
    logout(client)
    assert login(client, "user@example.com", "wrong").status_code == 401
    assert login(client, "user@example.com", "Passw0rd1").status_code == 302


def test_weak_password_and_duplicate_email_rejected(client):
    r = signup(client, password="short")
    assert r.status_code == 422
    signup(client)
    logout(client)
    r = signup(client)
    assert r.status_code == 422


def test_password_reset_token_is_single_use(client, app):
    signup(client)
    logout(client)
    from core import make_token
    u = User.query.filter_by(email="user@example.com").first()
    tok = make_token("reset", [u.id, u.password_hash[-12:]])
    from conftest import csrf_form
    form = csrf_form(client, f"/reset/{tok}")
    r = client.post(f"/reset/{tok}", data={"password": "NewPassw0rd9", "password2": "NewPassw0rd9", "csrf_token": form})
    assert r.status_code == 302
    r2 = client.get(f"/reset/{tok}")  # same token now invalid because the hash changed
    assert r2.status_code == 302 and "/forgot" in r2.headers["Location"]
    assert login(client, "user@example.com", "NewPassw0rd9").status_code == 302


def test_unverified_email_cannot_book(client, app):
    make_member(app, "unv@example.com", verified=False)
    c = make_class()
    login(client, "unv@example.com", "Passw0rd1")
    r, data = api_post(client, f"/api/classes/{c.id}/book")
    assert r.status_code == 403 and data["code"] == "email_unverified"


# ----------------------------------------------------------------- RBAC
def test_role_based_access(client, app):
    make_member(app, "m@example.com")
    create_admin("a@example.com", "AdminPass123")
    assert client.get("/member/").status_code == 302          # anonymous -> login
    login(client, "m@example.com", "Passw0rd1")
    assert client.get("/admin/").status_code == 403
    assert client.get("/trainer/").status_code == 403
    assert client.get("/member/").status_code == 200
    logout(client)
    login(client, "a@example.com", "AdminPass123")
    assert client.get("/admin/").status_code == 200


def test_member_cannot_touch_other_members_booking(client, app):
    a, b = make_member(app, "a1@example.com"), make_member(app, "b1@example.com")
    c = make_class(capacity=5)
    S.book_class(a.member, c.id)
    bid = ClassBooking.query.filter_by(member_id=a.member.id).first().id
    login(client, "b1@example.com", "Passw0rd1")
    r, _ = api_post(client, f"/api/bookings/{bid}/cancel")
    assert r.status_code == 404


# ----------------------------------------------------------------- security
def test_csrf_blocks_forged_post(client):
    r = client.post("/api/contact", json={"name": "x", "email": "x@x.com", "message": "hello"}, headers={"X-CSRF-Token": "bad"})
    assert r.status_code == 403


def test_open_redirect_blocked(client, app):
    make_member(app, "r@example.com")
    from conftest import csrf_form
    tok = csrf_form(client, "/login")
    r = client.post("/login?next=https://evil.example", data={"email": "r@example.com", "password": "Passw0rd1", "csrf_token": tok})
    assert "evil.example" not in r.headers["Location"]


def test_xss_is_escaped(client, app):
    make_member(app, "x1@example.com")
    create_admin("adm@example.com", "AdminPass123")
    api_post(client, "/api/contact", {"name": "<script>alert(1)</script>", "email": "z@example.com", "message": "hello there"})
    login(client, "adm@example.com", "AdminPass123")
    html = client.get("/admin/messages").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_upload_rejects_non_images(client, app):
    import io
    create_admin("adm2@example.com", "AdminPass123")
    login(client, "adm2@example.com", "AdminPass123")
    tok = csrf_meta(client)
    r = client.post("/admin/media", data={"csrf_token": tok, "files": (io.BytesIO(b"<?php ?>"), "x.php")},
                    content_type="multipart/form-data", follow_redirects=True)
    from models import Media
    assert Media.query.count() == 0
    r = client.post("/admin/media", data={"csrf_token": tok, "files": (io.BytesIO(b"not an image"), "x.jpg")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert Media.query.count() == 0


# ----------------------------------------------------------------- forms
def test_trial_validation_and_dedupe(client):
    ok_body = {"name": "Lead", "email": "lead@example.com", "phone": "+15551234567", "age": 28, "goal": "Build muscle",
               "preferred_date": (date.today() + timedelta(days=3)).isoformat(), "preferred_time": "Evening", "experience": "beginner"}
    r, d = api_post(client, "/api/trials", {**ok_body, "email": "not-an-email"})
    assert r.status_code == 422 and "email" in d["fields"]
    r, d = api_post(client, "/api/trials", {**ok_body, "preferred_date": "2001-01-01"})
    assert r.status_code == 422
    r, d = api_post(client, "/api/trials", ok_body)
    assert r.status_code == 200 and d["ok"]
    r, d = api_post(client, "/api/trials", ok_body)
    assert r.status_code == 409


def test_honeypot_silently_discards(client):
    from models import TrialLead
    r, d = api_post(client, "/api/trials", {"website": "http://spam", "name": "bot"})
    assert r.status_code == 200 and TrialLead.query.count() == 0


# ----------------------------------------------------------------- booking
def test_capacity_never_exceeded_and_waitlist_promotes(client, app):
    users = [make_member(app, f"u{i}@example.com") for i in range(4)]
    c = make_class(capacity=2)
    S.book_class(users[0].member, c.id)
    S.book_class(users[1].member, c.id)
    from core import ApiError
    with pytest.raises(ApiError) as e:
        S.book_class(users[2].member, c.id)
    assert e.value.code == "full"
    S.join_waitlist(users[2].member, c.id)
    S.join_waitlist(users[3].member, c.id)
    db.session.refresh(c)
    assert c.booked == 2 and c.is_full
    with pytest.raises(ApiError):  # duplicate booking blocked
        S.book_class(users[0].member, c.id)

    b = ClassBooking.query.filter_by(class_id=c.id, member_id=users[0].member.id).first()
    S.cancel_class_booking(b)
    offered = ClassBooking.query.filter_by(class_id=c.id, member_id=users[2].member.id).first()
    assert offered.status == "offered"                       # first in line gets it
    assert ClassBooking.query.filter_by(class_id=c.id, member_id=users[3].member.id).first().status == "waitlist"
    db.session.refresh(c)
    assert c.booked == 2                                     # spot is held, nobody can sneak in
    S.claim_offer(offered)
    assert offered.status == "booked"


def test_expired_waitlist_offer_moves_to_next(client, app):
    users = [make_member(app, f"w{i}@example.com") for i in range(3)]
    c = make_class(capacity=1)
    S.book_class(users[0].member, c.id)
    S.join_waitlist(users[1].member, c.id)
    S.join_waitlist(users[2].member, c.id)
    S.cancel_class_booking(ClassBooking.query.filter_by(member_id=users[0].member.id).first())
    first = ClassBooking.query.filter_by(member_id=users[1].member.id).first()
    first.offer_expires_at = datetime.now() - timedelta(minutes=1)
    db.session.commit()
    S.run_jobs()
    assert ClassBooking.query.filter_by(member_id=users[1].member.id).first().status == "expired"
    assert ClassBooking.query.filter_by(member_id=users[2].member.id).first().status == "offered"


def test_booking_requires_active_membership(client, app):
    u = make_member(app, "nomem@example.com", with_plan=False)
    c = make_class()
    from core import ApiError
    with pytest.raises(ApiError) as e:
        S.book_class(u.member, c.id)
    assert e.value.code == "membership_required"


def test_cancellation_cutoff(client, app):
    u = make_member(app, "cut@example.com")
    c = make_class(hours=1)  # starts in 1h, cutoff is 2h
    S.book_class(u.member, c.id)
    b = ClassBooking.query.filter_by(member_id=u.member.id).first()
    from core import ApiError
    with pytest.raises(ApiError):
        S.cancel_class_booking(b)
    S.cancel_class_booking(b, by_staff=True)


def test_trainer_slot_cannot_be_double_booked(client, app):
    seed_demo()
    t = Trainer.query.first()
    day = date.today() + timedelta(days=1)
    while day.weekday() > 4:
        day += timedelta(days=1)
    slots = S.slots_for(t, day)
    assert slots
    a, b = make_member(app, "p1@example.com"), make_member(app, "p2@example.com")
    S.book_session(a.member, t.id, slots[0])
    from core import ApiError
    with pytest.raises(ApiError) as e:
        S.book_session(b.member, t.id, slots[0])
    assert e.value.code == "slot_taken"
    assert slots[0] not in S.slots_for(t, day)


# ----------------------------------------------------------------- memberships & payments
def test_membership_lifecycle_and_expiry(client, app):
    u = make_member(app, "life@example.com")
    assert S.active_membership(u.member) is not None
    m = S.active_membership(u.member)
    m.end_date = date.today() - timedelta(days=1)
    db.session.commit()
    S.run_jobs()
    assert S.active_membership(u.member) is None
    assert Membership.query.get(m.id).status == "expired"


def test_payment_fulfillment_is_idempotent(client, app):
    u = make_member(app, "idem@example.com", with_plan=False)
    plan = MembershipPlan.query.first()
    m, pay = S.create_membership_purchase(u.member, plan, "monthly")
    S.fulfill_payment(pay, "T1")
    end = m.end_date
    S.fulfill_payment(pay, "T1")   # webhook retry must not extend the membership
    assert m.end_date == end and pay.status == "paid" and pay.receipt_no


def test_refund_cancels_membership(client, app):
    u = make_member(app, "ref@example.com")
    pay = Payment.query.filter_by(user_id=u.id).first()
    S.request_refund(pay)
    S.process_refund(pay)
    assert pay.status == "refunded" and S.active_membership(u.member) is None


def test_checkin_duplicate_protection(client, app):
    u = make_member(app, "chk@example.com")
    staff = create_admin("s@example.com", "AdminPass123")
    admin = User.query.filter_by(email="s@example.com").first()
    r = S.check_in(f"BM:{u.member.qr_token}", admin)
    assert r["action"] == "checked_in"
    from core import ApiError
    with pytest.raises(ApiError) as e:
        S.check_in(u.member.member_code, admin)
    assert e.value.code == "duplicate"


def test_quiz_recommendation(client):
    r, d = api_post(client, "/api/quiz", {"goal": "Build muscle", "experience": "beginner", "days_per_week": 4,
                                           "preferred_training": "Weights", "available_time": "Evening"})
    assert r.status_code == 200 and d["program"]
