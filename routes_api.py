from datetime import date, datetime, timedelta

from flask import Blueprint, current_app, request

import notifications as N
import services as S
from core import (ApiError, Form, current_user, login_required, ok, payload, ratelimit, require_member,
                  require_verified)
from models import (ClassBooking, ContactMessage, FitnessGoal, FitnessProgress, GymClass, Member, Membership,
                    MembershipPlan, Payment, QuizResponse, TourBooking, Trainer, TrainerSession, TrialLead, db)

bp = Blueprint("api", __name__, url_prefix="/api")

GOALS = ["Build muscle", "Lose weight", "Improve strength", "Improve endurance", "Improve mobility", "General fitness"]
LEVELS = ["beginner", "intermediate", "advanced"]
TIMES = ["Morning", "Midday", "Evening", "Late night"]


def class_json(c):
    return {"id": c.id, "name": c.name, "category": c.category, "starts_at": c.starts_at.isoformat(),
            "when": c.starts_at.strftime("%a %d %b · %I:%M %p"), "duration": c.duration_minutes,
            "location": c.location, "difficulty": c.difficulty, "trainer": c.trainer.name if c.trainer else None,
            **S.availability(c)}


# ================================================================== PUBLIC
@bp.route("/trials", methods=["POST"])
@ratelimit(4, 3600)
def trial():
    d = payload()
    if d.get("website"):  # honeypot: real users never see or fill this field
        return ok(message="Thanks! We'll be in touch shortly.")
    f = Form(d)
    f.text("name", "Name", max=120, min=2)
    f.email()
    f.phone()
    f.int_("age", "Age", min=12, max=90)
    f.choice("goal", GOALS, "goal")
    f.date("preferred_date", "Preferred date", future=True)
    f.choice("preferred_time", TIMES, "preferred time")
    f.choice("experience", LEVELS, "experience level")
    f.check()
    v = f.v
    if TrialLead.query.filter(TrialLead.email == v["email"], TrialLead.created_at > datetime.now() - timedelta(days=1)).first():
        raise ApiError("We already have your trial request from today — we'll be in touch soon!", 409)
    lead = TrialLead(name=v["name"], email=v["email"], phone=v["phone"], age=v["age"], goal=v["goal"],
                     preferred_date=v["preferred_date"], preferred_time=v["preferred_time"], experience=v["experience"])
    db.session.add(lead)
    db.session.commit()
    N.send_email(lead.email, "Your free trial request", N.email_html(
        f"You're on the list, {lead.name.split()[0]}",
        [f"We've received your free trial request for {lead.preferred_date:%A %d %B} ({lead.preferred_time.lower()}).",
         "A coach will contact you shortly to confirm your session. Bring water and a towel."]))
    N.notify_admins("lead", "New free trial lead", f"{lead.name} · {lead.phone} · goal: {lead.goal}", "/admin/leads")
    S.track("trial_submit")
    return ok(message="You're in! Check your email — a coach will contact you shortly to confirm your session.")


@bp.route("/tours", methods=["POST"])
@ratelimit(4, 3600)
def tour():
    d = payload()
    if d.get("website"):
        return ok(message="Thanks! Your tour request is in.")
    f = Form(d)
    f.text("name", "Name", max=120, min=2)
    f.email()
    f.phone()
    f.date("tour_date", "Date", future=True)
    f.choice("tour_time", ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00"], "time")
    f.check()
    v = f.v
    if TourBooking.query.filter(TourBooking.tour_date == v["tour_date"], TourBooking.tour_time == v["tour_time"],
                                TourBooking.status.in_(("pending", "approved"))).count() >= 2:
        raise ApiError("That tour time is fully booked. Please choose another time.", 409)
    t = TourBooking(name=v["name"], email=v["email"], phone=v["phone"], tour_date=v["tour_date"], tour_time=v["tour_time"])
    db.session.add(t)
    db.session.commit()
    N.send_email(t.email, "Tour request received", N.email_html(
        "Your tour request is in", [f"{t.tour_date:%A %d %B} at {t.tour_time}. We'll confirm shortly by email or phone."]))
    N.notify_admins("tour", "New tour request", f"{t.name} · {t.tour_date:%d %b} {t.tour_time} · {t.phone}", "/admin/tours")
    S.track("tour_booking")
    return ok(message="Tour requested! We'll confirm by email shortly.")


@bp.route("/contact", methods=["POST"])
@ratelimit(5, 3600)
def contact():
    d = payload()
    if d.get("website"):
        return ok(message="Message sent. Thank you!")
    f = Form(d)
    f.text("name", "Name", max=120, min=2)
    f.email()
    f.phone(required=False)
    f.text("message", "Message", max=2000, min=5)
    f.check()
    v = f.v
    db.session.add(ContactMessage(name=v["name"], email=v["email"], phone=v["phone"], message=v["message"]))
    db.session.commit()
    N.notify_admins("contact", "New contact message", f"{v['name']}: {v['message'][:140]}", "/admin/messages")
    return ok(message="Message sent — we'll get back to you within one business day.")


@bp.route("/quiz", methods=["POST"])
@ratelimit(20, 3600)
def quiz():
    f = Form(payload())
    f.choice("goal", GOALS, "goal")
    f.choice("experience", LEVELS, "experience level")
    f.int_("days_per_week", "Days per week", min=1, max=7)
    f.choice("preferred_training", ["Weights", "Classes", "Cardio", "Boxing", "Mobility", "Mixed"], "training style")
    f.choice("available_time", TIMES, "available time")
    f.check()
    v, u = f.v, current_user()
    db.session.add(QuizResponse(user_id=u.id if u else None, goal=v["goal"], experience=v["experience"], days_per_week=v["days_per_week"],
                                preferred_training=v["preferred_training"], available_time=v["available_time"]))
    db.session.commit()
    r = S.recommend(v["goal"], v["experience"], v["days_per_week"])
    p, t = r["plan"], r["trainer"]
    return ok(program=r["program"],
              plan={"id": p.id, "name": p.name, "price": p.price_monthly_cents / 100} if p else None,
              trainer={"id": t.id, "name": t.name, "specialization": t.specialization} if t else None,
              classes=[class_json(c) for c in r["classes"]])


@bp.route("/classes")
def classes():
    q = GymClass.query.filter(GymClass.status == "scheduled", GymClass.starts_at > datetime.now())
    if request.args.get("category"):
        q = q.filter(GymClass.category == request.args["category"])
    if request.args.get("trainer_id", type=int):
        q = q.filter(GymClass.trainer_id == request.args.get("trainer_id", type=int))
    if request.args.get("date"):
        try:
            d = datetime.strptime(request.args["date"], "%Y-%m-%d")
            q = q.filter(GymClass.starts_at >= d, GymClass.starts_at < d + timedelta(days=1))
        except ValueError:
            raise ApiError("Invalid date.", 422)
    return ok(classes=[class_json(c) for c in q.order_by(GymClass.starts_at).limit(60)])


@bp.route("/classes/availability")
def availability():
    ids = [int(i) for i in (request.args.get("ids") or "").split(",") if i.isdigit()][:60]
    rows = GymClass.query.filter(GymClass.id.in_(ids)).all() if ids else []
    return ok(items={str(c.id): S.availability(c) for c in rows})


@bp.route("/trainers/<int:tid>/slots")
def slots(tid):
    t = db.session.get(Trainer, tid)
    if not t or not t.active:
        raise ApiError("Trainer not found.", 404)
    try:
        day = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        raise ApiError("Choose a valid date.", 422)
    if day < date.today() or day > date.today() + timedelta(days=60):
        raise ApiError("Choose a date within the next 60 days.", 422)
    return ok(slots=[{"start": s.isoformat(), "label": s.strftime("%I:%M %p").lstrip("0")} for s in S.slots_for(t, day)],
              price=t.session_price_cents / 100, minutes=t.session_minutes)


# ================================================================== MEMBER
@bp.route("/classes/<int:cid>/book", methods=["POST"])
@login_required("member")
def book(cid):
    m = require_member()
    require_verified(m.user)
    b, c = S.book_class(m, cid)
    return ok(message="Your class has been booked successfully.", availability=S.availability(c))


@bp.route("/classes/<int:cid>/waitlist", methods=["POST"])
@login_required("member")
def waitlist(cid):
    m = require_member()
    require_verified(m.user)
    pos = S.join_waitlist(m, cid)
    return ok(message=f"You're on the waitlist (position {pos}). We'll notify you the moment a spot opens.")


def _own_booking(bid):
    m = require_member()
    b = db.session.get(ClassBooking, bid)
    if not b or b.member_id != m.id:
        raise ApiError("Booking not found.", 404)
    return b


@bp.route("/bookings/<int:bid>/cancel", methods=["POST"])
@login_required("member")
def cancel_booking(bid):
    S.cancel_class_booking(_own_booking(bid))
    return ok(message="Your booking has been cancelled.")


@bp.route("/bookings/<int:bid>/claim", methods=["POST"])
@login_required("member")
def claim(bid):
    S.claim_offer(_own_booking(bid))
    return ok(message="Spot claimed — see you in class!")


@bp.route("/sessions", methods=["POST"])
@login_required("member")
def create_session():
    m = require_member()
    require_verified(m.user)
    f = Form(payload())
    f.int_("trainer_id", "Trainer", min=1)
    f.text("start", "Time slot", max=30)
    f.choice("service", ["Personal Training", "Fitness Assessment", "Mobility Session"], "service")
    f.check()
    try:
        start = datetime.fromisoformat(f.v["start"])
    except ValueError:
        raise ApiError("Choose a valid time slot.", 422)
    s, pay = S.book_session(m, f.v["trainer_id"], start, f.v["service"])
    if pay:
        return ok(message="Slot held for 30 minutes — complete payment to confirm it.", payment_id=pay.id)
    return ok(message="Your training session has been booked successfully.")


@bp.route("/sessions/<int:sid>/cancel", methods=["POST"])
@login_required("member", "trainer", "admin")
def cancel_session(sid):
    u, s = current_user(), db.session.get(TrainerSession, sid)
    if not s:
        raise ApiError("Session not found.", 404)
    staff = u.role_name == "admin" or (u.trainer and u.trainer.id == s.trainer_id)
    if not staff and not (u.member and u.member.id == s.member_id):
        raise ApiError("Session not found.", 404)
    S.cancel_session(s, by_staff=bool(staff))
    return ok(message="Session cancelled.")


@bp.route("/memberships", methods=["POST"])
@login_required("member")
def buy_membership():
    m = require_member()
    require_verified(m.user)
    f = Form(payload())
    f.int_("plan_id", "Plan", min=1)
    f.choice("billing", ["monthly", "yearly"], "billing")
    f.check()
    plan = db.session.get(MembershipPlan, f.v["plan_id"])
    if not plan:
        raise ApiError("Plan not found.", 404)
    mem, pay = S.create_membership_purchase(m, plan, f.v["billing"])
    return ok(payment_id=pay.id, kind=mem.kind)


@bp.route("/memberships/<int:mid>/cancel", methods=["POST"])
@login_required("member")
def cancel_membership(mid):
    m = require_member()
    mem = db.session.get(Membership, mid)
    if not mem or mem.member_id != m.id:
        raise ApiError("Membership not found.", 404)
    S.cancel_membership(mem)
    return ok(message=f"Your membership won't renew. You keep full access until {mem.end_date:%d %b %Y}.")


def _own_payment(pid):
    pay = db.session.get(Payment, pid)
    u = current_user()
    if not pay or (pay.user_id != u.id and u.role_name != "admin"):
        raise ApiError("Payment not found.", 404)
    return pay


@bp.route("/payments/<int:pid>/checkout", methods=["POST"])
@login_required("member")
def checkout(pid):
    return ok(**S.start_checkout(_own_payment(pid)))


@bp.route("/payments/<int:pid>/refund-request", methods=["POST"])
@login_required("member")
def refund_request(pid):
    S.request_refund(_own_payment(pid))
    return ok(message="Refund requested. We'll review it and email you the outcome.")


@bp.route("/progress", methods=["POST"])
@login_required("member")
def add_progress():
    m = require_member()
    d = payload()
    f = Form(d)
    metric = (d.get("metric") or "").strip()
    if metric == "pr":
        lift = (d.get("lift") or "").strip().lower()[:30]
        if not lift:
            raise ApiError("Enter the exercise name for your personal record.", 422)
        metric, unit = f"pr:{lift}", "kg"
    else:
        f.choice("metric", ["weight", "body_fat", "chest", "waist", "hips", "arm", "thigh"], "measurement")
        unit = "kg" if metric == "weight" else ("%" if metric == "body_fat" else "cm")
    f.float_("value", "Value", min=0.1, max=1000)
    f.date("recorded_on", "Date", required=False, past=True)
    f.text("note", "Note", required=False, max=200)
    f.check()
    db.session.add(FitnessProgress(member_id=m.id, metric=metric, value=f.v["value"], unit=unit,
                                   recorded_on=f.v["recorded_on"] or date.today(), note=f.v["note"] or None))
    db.session.commit()
    return ok(message="Progress saved.")


@bp.route("/progress/<int:pid>/delete", methods=["POST"])
@login_required("member")
def del_progress(pid):
    m = require_member()
    p = db.session.get(FitnessProgress, pid)
    if not p or p.member_id != m.id:
        raise ApiError("Entry not found.", 404)
    db.session.delete(p)
    db.session.commit()
    return ok(message="Entry removed.")


@bp.route("/goals", methods=["POST"])
@login_required("member")
def add_goal():
    m = require_member()
    f = Form(payload())
    f.text("goal", "Goal", max=200, min=3)
    f.date("target_date", "Target date", required=False, future=True)
    f.check()
    db.session.add(FitnessGoal(member_id=m.id, goal=f.v["goal"], target_date=f.v["target_date"]))
    db.session.commit()
    return ok(message="Goal added.")


@bp.route("/goals/<int:gid>/status", methods=["POST"])
@login_required("member")
def goal_status(gid):
    m = require_member()
    g = db.session.get(FitnessGoal, gid)
    f = Form(payload())
    f.choice("status", ["active", "achieved", "dropped"], "status")
    f.check()
    if not g or g.member_id != m.id:
        raise ApiError("Goal not found.", 404)
    g.status = f.v["status"]
    db.session.commit()
    return ok(message="Goal updated.")


@bp.route("/progress/series")
@login_required
def series():
    u = current_user()
    mid = request.args.get("member_id", type=int) or (u.member.id if u.member else None)
    member = db.session.get(Member, mid) if mid else None
    if not member or not S.can_view_member(u, member):
        raise ApiError("You don't have access to that member's progress.", 403)
    metric = request.args.get("metric", "weight")
    return ok(series=S.progress_series(member.id, metric), weekly=S.weekly_attendance(member.id))


# ================================================================== STAFF
@bp.route("/attendance/check", methods=["POST"])
@login_required("admin", "trainer")
def attendance_check():
    d = payload()
    res = S.check_in(d.get("code"), current_user(), force=bool(d.get("force")) and current_user().role_name == "admin")
    return ok(**res)


@bp.route("/class-bookings/<int:bid>/status", methods=["POST"])
@login_required("admin", "trainer")
def booking_status(bid):
    b, u = db.session.get(ClassBooking, bid), current_user()
    if not b or (u.role_name == "trainer" and b.gym_class.trainer_id != (u.trainer.id if u.trainer else -1)):
        raise ApiError("Booking not found.", 404)
    f = Form(payload())
    f.choice("status", ["attended", "no_show", "cancelled"], "status")
    f.check()
    if f.v["status"] == "cancelled":
        S.cancel_class_booking(b, by_staff=True)
    else:
        b.status = f.v["status"]
        db.session.commit()
    return ok(message="Updated.")


@bp.route("/sessions/<int:sid>/status", methods=["POST"])
@login_required("admin", "trainer")
def session_status(sid):
    s, u = db.session.get(TrainerSession, sid), current_user()
    if not s or (u.role_name == "trainer" and s.trainer_id != (u.trainer.id if u.trainer else -1)):
        raise ApiError("Session not found.", 404)
    f = Form(payload())
    f.choice("status", ["completed", "no_show", "cancelled"], "status")
    f.check()
    if f.v["status"] == "cancelled":
        S.cancel_session(s, by_staff=True)
    else:
        s.status = f.v["status"]
        db.session.commit()
    return ok(message="Updated.")


@bp.route("/notifications/read", methods=["POST"])
@login_required
def read_notifications():
    from models import Notification
    Notification.query.filter_by(user_id=current_user().id, read_at=None).update({"read_at": datetime.now()})
    db.session.commit()
    return ok()


# ================================================================ PAYMENTS
@bp.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Signature-verified. This (not the browser redirect) is the source of truth for 'paid'."""
    cfg = current_app.config
    if not (cfg["STRIPE_SECRET_KEY"] and cfg["STRIPE_WEBHOOK_SECRET"]):
        raise ApiError("Payments webhook is not configured.", 503)
    import stripe
    try:
        event = stripe.Webhook.construct_event(request.get_data(), request.headers.get("Stripe-Signature", ""),
                                               cfg["STRIPE_WEBHOOK_SECRET"])
    except Exception:
        raise ApiError("Invalid signature.", 400)
    obj = event["data"]["object"]
    pid = (obj.get("metadata") or {}).get("payment_id")
    pay = db.session.get(Payment, int(pid)) if pid and str(pid).isdigit() else None
    if pay:
        t = event["type"]
        if t == "checkout.session.completed" and obj.get("payment_status") == "paid":
            S.fulfill_payment(pay, obj.get("payment_intent"))
        elif t in ("checkout.session.expired", "checkout.session.async_payment_failed"):
            S.fail_payment(pay, "Checkout expired or failed")
    return ok()
