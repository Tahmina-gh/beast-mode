"""Business workflow. Routes stay thin; every rule lives here so it's enforced everywhere."""
from datetime import date, datetime, time, timedelta

from flask import current_app
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

import notifications as N
from core import ApiError
from models import (Attendance, ClassBooking, FitnessProgress, GymClass, Member, Membership, MembershipPlan,
                    Payment, QuizRule, Trainer, TrainerSession, AnalyticsEvent, db)

ACTIVE_BOOKING = ("booked", "offered", "attended")


def track(name, path=None):
    db.session.add(AnalyticsEvent(name=name, path=(path or "")[:120]))
    db.session.commit()


def fmt_dt(dt):
    return dt.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


# ================================================================ memberships
def active_membership(member, on=None):
    t = on or date.today()
    return (Membership.query.filter(Membership.member_id == member.id, Membership.status == "active",
                                    Membership.start_date <= t, Membership.end_date >= t)
            .order_by(Membership.end_date.desc()).first())


def upcoming_membership(member):
    return (Membership.query.filter(Membership.member_id == member.id, Membership.status == "active",
                                    Membership.start_date > date.today()).order_by(Membership.start_date).first())


def membership_info(m):
    if not m:
        return None
    left = (m.end_date - date.today()).days
    return {"days_left": left, "expiring": left <= 7}


def create_membership_purchase(member, plan, billing):
    if plan.status != "active":
        raise ApiError("That plan isn't available right now.", 409)
    if billing not in ("monthly", "yearly"):
        raise ApiError("Choose monthly or yearly billing.", 422)
    price = plan.price_yearly_cents if billing == "yearly" else plan.price_monthly_cents
    cur = active_membership(member)
    kind = "new"
    if cur:
        cp = cur.plan.price_monthly_cents
        kind = "renew" if cur.plan_id == plan.id else ("upgrade" if plan.price_monthly_cents > cp else "downgrade")
    # reuse an unpaid draft for the same plan instead of piling up duplicates
    Membership.query.filter_by(member_id=member.id, status="pending").delete()
    m = Membership(member_id=member.id, plan_id=plan.id, billing=billing, kind=kind, status="pending")
    db.session.add(m)
    db.session.flush()
    pay = Payment(user_id=member.user_id, purpose="membership", membership_id=m.id, amount_cents=price,
                  currency=current_app.config["CURRENCY"], status="pending",
                  description=f"{plan.name} membership ({billing})")
    db.session.add(pay)
    db.session.commit()
    return m, pay


def activate_membership(m):
    plan, member, today = m.plan, m.member, date.today()
    days = 365 if m.billing == "yearly" else plan.duration_days
    cur = active_membership(member)
    if cur and cur.id != m.id:
        if m.kind == "upgrade":
            cur.status, cur.end_date = "replaced", today
            start = today
        else:  # renew / downgrade start when the current period ends
            start = cur.end_date + timedelta(days=1)
    else:
        start = today
    m.start_date, m.end_date, m.renewal_date = start, start + timedelta(days=days), start + timedelta(days=days)
    m.status, m.payment_status, m.reminders_sent = "active", "paid", ""


def cancel_membership(m):
    if m.status != "active":
        raise ApiError("Only an active membership can be cancelled.", 409)
    m.cancel_at_period_end = True
    db.session.commit()


# ==================================================================== payments
def stripe_enabled():
    return bool(current_app.config["STRIPE_SECRET_KEY"])


def _stripe():
    import stripe
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    return stripe


def start_checkout(pay):
    if pay.status == "paid":
        raise ApiError("This payment is already complete.", 409)
    if pay.status == "refunded":
        raise ApiError("This payment was refunded.", 409)
    base = current_app.config["BASE_URL"]
    if stripe_enabled():
        stripe = _stripe()
        try:
            s = stripe.checkout.Session.create(
                mode="payment", client_reference_id=str(pay.id), customer_email=pay.user.email,
                metadata={"payment_id": str(pay.id)},
                line_items=[{"quantity": 1, "price_data": {"currency": pay.currency, "unit_amount": pay.amount_cents,
                                                           "product_data": {"name": pay.description or "Payment"}}}],
                success_url=f"{base}/member/payments/return?pid={pay.id}&session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{base}/member/payments/return?pid={pay.id}&cancelled=1")
        except Exception as e:
            pay.status, pay.failure_reason = "failed", str(e)[:190]
            db.session.commit()
            raise ApiError("We couldn't start the payment. Please try again.", 502)
        pay.provider, pay.provider_ref, pay.status = "stripe", s.id, "pending"
        db.session.commit()
        return {"url": s.url}
    pay.provider, pay.status = "manual", "pending"
    pay.transaction_id = pay.transaction_id or f"BM-{pay.id:06d}"
    db.session.commit()
    return {"manual": True, "reference": pay.transaction_id,
            "message": f"Pay at the front desk quoting reference {pay.transaction_id}. "
                       "Your booking is confirmed the moment staff mark it paid."}


def fulfill_payment(pay, transaction_id=None):
    """Idempotent: safe to call from webhook, return URL and admin action."""
    if pay.status == "paid":
        return
    pay.status, pay.paid_at, pay.failure_reason = "paid", datetime.now(), None
    pay.receipt_no = f"BM-{datetime.now():%Y%m%d}-{pay.id:05d}"
    if transaction_id:
        pay.transaction_id = transaction_id
    user = pay.user
    if pay.membership:
        activate_membership(pay.membership)
        db.session.commit()
        m = pay.membership
        N.notify(user, "membership", f"Welcome to {m.plan.name}",
                 f"Your {m.plan.name} membership is active until {m.end_date:%d %b %Y}.", "/member/membership")
        track("membership_purchase")
    if pay.session and pay.session.status == "pending_payment":
        pay.session.status = "confirmed"
        db.session.commit()
        s = pay.session
        N.notify(user, "trainer_booking", "Training session confirmed",
                 f"{s.service} with {s.trainer.name} on {fmt_dt(s.starts_at)}.", "/member/bookings", sms=True)
    db.session.commit()
    N.notify(user, "payment", "Payment received",
             f"We received {pay.amount_cents / 100:.2f} {pay.currency.upper()} for {pay.description}. Receipt {pay.receipt_no}.",
             f"/member/payments/{pay.id}/receipt", cta="View receipt")


def fail_payment(pay, reason="Payment could not be completed"):
    if pay.status == "pending":
        pay.status, pay.failure_reason = "failed", reason[:190]
        db.session.commit()
        N.notify(pay.user, "payment_failed", "Your payment could not be completed",
                 "No money was taken. You can retry from your payments page.", "/member/payments", cta="Try again")


def request_refund(pay):
    if pay.status != "paid":
        raise ApiError("Only completed payments can be refunded.", 409)
    if pay.refund_status != "none":
        raise ApiError("A refund has already been requested for this payment.", 409)
    pay.refund_status = "requested"
    db.session.commit()
    N.notify_admins("refund", "Refund requested", f"{pay.user.full_name} requested a refund for {pay.description}.", "/admin/payments")


def process_refund(pay):
    if pay.status != "paid":
        raise ApiError("Only paid payments can be refunded.", 409)
    if pay.provider == "stripe" and stripe_enabled() and pay.transaction_id:
        try:
            _stripe().Refund.create(payment_intent=pay.transaction_id)
        except Exception as e:
            raise ApiError(f"Stripe refused the refund: {str(e)[:120]}", 502)
    pay.status, pay.refund_status = "refunded", "refunded"
    if pay.membership and pay.membership.status == "active":
        pay.membership.status, pay.membership.end_date = "cancelled", date.today() - timedelta(days=1)
    if pay.session and pay.session.status in ("confirmed", "pending_payment"):
        pay.session.status, pay.session.slot_lock = "cancelled", None
    db.session.commit()
    N.notify(pay.user, "refund", "Your refund was processed", f"{pay.description} has been refunded.", "/member/payments")


# ================================================================ class booking
def month_bounds(d):
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return datetime.combine(first, time.min), datetime.combine(nxt, time.min)


def classes_used(member, when):
    a, b = month_bounds(when.date())
    return (db.session.query(func.count(ClassBooking.id)).join(GymClass, GymClass.id == ClassBooking.class_id)
            .filter(ClassBooking.member_id == member.id, ClassBooking.status.in_(ACTIVE_BOOKING),
                    GymClass.starts_at >= a, GymClass.starts_at < b).scalar() or 0)


def availability(c):
    return {"id": c.id, "booked": c.booked, "capacity": c.capacity, "remaining": c.remaining,
            "full": c.is_full or c.status != "scheduled", "status": c.status,
            "label": f"{c.booked} / {c.capacity} spots booked",
            "remaining_label": "FULLY BOOKED" if c.is_full else f"{c.remaining} spot{'s' if c.remaining != 1 else ''} remaining"}


def _check_bookable(member, cls):
    if not cls or cls.status != "scheduled":
        raise ApiError("This class isn't available.", 404)
    if cls.starts_at <= datetime.now():
        raise ApiError("This class has already started.", 409)
    m = active_membership(member)
    if not m:
        raise ApiError("You need an active membership to book classes.", 403, "membership_required")
    if m.plan.classes_included >= 0 and classes_used(member, cls.starts_at) >= m.plan.classes_included:
        raise ApiError(f"Your {m.plan.name} plan includes {m.plan.classes_included} classes a month and you've used them all.", 403)
    clash = (db.session.query(ClassBooking.id).join(GymClass, GymClass.id == ClassBooking.class_id)
             .filter(ClassBooking.member_id == member.id, ClassBooking.status.in_(("booked", "offered")),
                     GymClass.starts_at < cls.ends_at, GymClass.ends_at > cls.starts_at, GymClass.id != cls.id).first())
    if clash:
        raise ApiError("You already have a class booked at that time.", 409)


def book_class(member, class_id):
    cls = db.session.get(GymClass, class_id)
    _check_bookable(member, cls)
    ex = ClassBooking.query.filter_by(class_id=class_id, member_id=member.id).first()
    if ex and ex.status in ACTIVE_BOOKING:
        raise ApiError("You've already booked this class.", 409)
    if ex and ex.status == "waitlist":
        raise ApiError("You're already on the waitlist for this class.", 409)
    # Atomic capacity guard: the WHERE clause makes overbooking impossible even under concurrency.
    res = db.session.execute(update(GymClass).where(GymClass.id == class_id, GymClass.status == "scheduled",
                                                    GymClass.booked < GymClass.capacity)
                             .values(booked=GymClass.booked + 1))
    if res.rowcount == 0:
        db.session.rollback()
        raise ApiError("This class is fully booked.", 409, "full")
    if ex:
        ex.status, ex.created_at, ex.cancelled_at, ex.offer_expires_at, ex.reminded = "booked", datetime.now(), None, None, False
        b = ex
    else:
        b = ClassBooking(class_id=class_id, member_id=member.id, status="booked")
        db.session.add(b)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError("You've already booked this class.", 409)
    db.session.refresh(cls)
    N.notify(member.user, "class_booking", "Class booked",
             f"{cls.name} on {fmt_dt(cls.starts_at)} at {cls.location}.", "/member/bookings")
    track("class_booking")
    return b, cls


def join_waitlist(member, class_id):
    cls = db.session.get(GymClass, class_id)
    _check_bookable(member, cls)
    if not cls.is_full:
        raise ApiError("There are spots available — you can book directly.", 409)
    ex = ClassBooking.query.filter_by(class_id=class_id, member_id=member.id).first()
    if ex and ex.status in ACTIVE_BOOKING + ("waitlist",):
        raise ApiError("You're already booked or waitlisted for this class.", 409)
    if ex:
        ex.status, ex.created_at, ex.cancelled_at = "waitlist", datetime.now(), None
    else:
        db.session.add(ClassBooking(class_id=class_id, member_id=member.id, status="waitlist"))
    db.session.commit()
    pos = ClassBooking.query.filter_by(class_id=class_id, status="waitlist").count()
    N.notify(member.user, "waitlist", "You're on the waitlist",
             f"{cls.name} on {fmt_dt(cls.starts_at)}. We'll alert you the moment a spot opens (position {pos}).",
             "/member/bookings", email=False)
    return pos


def _free_spot(class_id):
    db.session.execute(update(GymClass).where(GymClass.id == class_id, GymClass.booked > 0)
                       .values(booked=GymClass.booked - 1))


def cancel_class_booking(b, by_staff=False):
    if b.status not in ("booked", "offered", "waitlist"):
        raise ApiError("This booking can't be cancelled.", 409)
    cls = b.gym_class
    if b.status == "booked" and not by_staff:
        cutoff = timedelta(hours=current_app.config["CANCEL_CUTOFF_HOURS"])
        if cls.starts_at - datetime.now() < cutoff:
            raise ApiError(f"Bookings can only be cancelled up to {current_app.config['CANCEL_CUTOFF_HOURS']} hours before the class.", 409)
    held = b.status in ("booked", "offered")
    b.status, b.cancelled_at = "cancelled", datetime.now()
    if held:
        _free_spot(b.class_id)
    db.session.commit()
    if held:
        promote_waitlist(b.class_id)
    if by_staff:
        N.notify(b.member.user, "class_cancel", "Your class booking was cancelled",
                 f"{cls.name} on {fmt_dt(cls.starts_at)} was cancelled by the gym.", "/member/classes")


def promote_waitlist(class_id):
    """When a spot frees up, offer it to the next person. The spot is held (counted in `booked`) until they claim or it expires."""
    cls = db.session.get(GymClass, class_id)
    if not cls or cls.status != "scheduled" or cls.starts_at <= datetime.now():
        return
    while True:
        nxt = (ClassBooking.query.filter_by(class_id=class_id, status="waitlist")
               .order_by(ClassBooking.created_at, ClassBooking.id).first())
        if not nxt:
            return
        if not active_membership(nxt.member):
            nxt.status = "cancelled"
            db.session.commit()
            continue
        res = db.session.execute(update(GymClass).where(GymClass.id == class_id, GymClass.booked < GymClass.capacity)
                                 .values(booked=GymClass.booked + 1))
        if res.rowcount == 0:
            db.session.rollback()
            return
        hours = current_app.config["WAITLIST_OFFER_HOURS"]
        nxt.status = "offered"
        nxt.offer_expires_at = min(datetime.now() + timedelta(hours=hours), cls.starts_at)
        db.session.commit()
        N.notify(nxt.member.user, "waitlist_offer", "A spot just opened up!",
                 f"{cls.name} on {fmt_dt(cls.starts_at)}. Claim it within {hours} hours or it goes to the next person.",
                 "/member/bookings", cta="Claim my spot", sms=True)
        return


def claim_offer(b):
    if b.status != "offered":
        raise ApiError("There's no spot waiting to be claimed.", 409)
    if b.offer_expires_at and b.offer_expires_at < datetime.now():
        raise ApiError("Sorry, this offer has expired.", 409)
    b.status, b.offer_expires_at = "booked", None
    db.session.commit()
    N.notify(b.member.user, "class_booking", "Spot claimed",
             f"You're in: {b.gym_class.name} on {fmt_dt(b.gym_class.starts_at)}.", "/member/bookings")
    track("class_booking")


def cancel_class(cls):
    """Admin cancels a whole class: notify everyone and release."""
    cls.status = "cancelled"
    for b in list(cls.bookings):
        if b.status in ("booked", "offered", "waitlist"):
            b.status, b.cancelled_at = "cancelled", datetime.now()
            N.notify(b.member.user, "class_cancel", "Class cancelled",
                     f"{cls.name} on {fmt_dt(cls.starts_at)} has been cancelled. Sorry about that.", "/member/classes")
    cls.booked = 0
    db.session.commit()


# ============================================================ trainer sessions
def slots_for(trainer, day):
    if not trainer.active:
        return []
    mins = trainer.session_minutes
    taken = {s.starts_at for s in TrainerSession.query.filter(
        TrainerSession.trainer_id == trainer.id, TrainerSession.status.in_(("confirmed", "pending_payment")),
        TrainerSession.starts_at >= datetime.combine(day, time.min),
        TrainerSession.starts_at < datetime.combine(day + timedelta(days=1), time.min))}
    busy = [(c.starts_at, c.ends_at) for c in GymClass.query.filter(
        GymClass.trainer_id == trainer.id, GymClass.status == "scheduled",
        GymClass.starts_at >= datetime.combine(day, time.min),
        GymClass.starts_at < datetime.combine(day + timedelta(days=1), time.min))]
    out, lead = [], datetime.now() + timedelta(hours=1)
    for w in (a for a in trainer.availability if a.weekday == day.weekday()):
        t, end = datetime.combine(day, w.start_time), datetime.combine(day, w.end_time)
        while t + timedelta(minutes=mins) <= end:
            e = t + timedelta(minutes=mins)
            if t > lead and t not in taken and not any(t < be and e > bs for bs, be in busy):
                out.append(t)
            t += timedelta(minutes=mins)
    return sorted(out)


def sessions_included_used(member, when):
    a, b = month_bounds(when.date())
    return (TrainerSession.query.filter(TrainerSession.member_id == member.id, TrainerSession.included.is_(True),
                                        TrainerSession.status.in_(("confirmed", "completed")),
                                        TrainerSession.starts_at >= a, TrainerSession.starts_at < b).count())


def book_session(member, trainer_id, start, service="Personal Training"):
    trainer = db.session.get(Trainer, trainer_id)
    if not trainer or not trainer.active:
        raise ApiError("That trainer isn't available.", 404)
    if start not in slots_for(trainer, start.date()):
        raise ApiError("That time slot is no longer available.", 409, "slot_taken")
    m = active_membership(member)
    included = bool(m and m.plan.pt_sessions > sessions_included_used(member, start) and m.plan.pt_sessions > 0)
    price = 0 if included else trainer.session_price_cents
    status = "confirmed" if price == 0 else "pending_payment"
    s = TrainerSession(member_id=member.id, trainer_id=trainer.id, service=service, starts_at=start,
                       ends_at=start + timedelta(minutes=trainer.session_minutes), price_cents=price,
                       included=included, status=status, slot_lock=f"{trainer.id}|{start.isoformat()}")
    db.session.add(s)
    pay = None
    try:
        db.session.flush()
        if price > 0:
            pay = Payment(user_id=member.user_id, purpose="session", session_id=s.id, amount_cents=price,
                          currency=current_app.config["CURRENCY"],
                          description=f"{service} with {trainer.name}, {fmt_dt(start)}")
            db.session.add(pay)
        db.session.commit()
    except IntegrityError:  # unique slot_lock: someone else got it first
        db.session.rollback()
        raise ApiError("That time slot is no longer available.", 409, "slot_taken")
    if status == "confirmed":
        N.notify(member.user, "trainer_booking", "Training session booked",
                 f"{service} with {trainer.name} on {fmt_dt(start)}.", "/member/bookings", sms=True)
        N.notify(trainer.user, "trainer_booking", "New session booked",
                 f"{member.user.full_name} booked {fmt_dt(start)}.", "/trainer/schedule", email=False)
        track("trainer_booking")
    else:
        track("trainer_booking")
    return s, pay


def cancel_session(s, by_staff=False):
    if s.status not in ("confirmed", "pending_payment"):
        raise ApiError("This session can't be cancelled.", 409)
    if s.status == "confirmed" and not by_staff and s.starts_at - datetime.now() < timedelta(hours=24):
        raise ApiError("Training sessions can be cancelled up to 24 hours before they start.", 409)
    s.status, s.slot_lock = "cancelled", None
    db.session.commit()
    N.notify(s.member.user, "trainer_cancel", "Training session cancelled",
             f"{s.service} on {fmt_dt(s.starts_at)} was cancelled.", "/member/bookings")
    if by_staff is False:
        N.notify(s.trainer.user, "trainer_cancel", "Session cancelled", f"{s.member.user.full_name} cancelled {fmt_dt(s.starts_at)}.",
                 "/trainer/schedule", email=False)


def reschedule_session(s, new_start):
    if new_start not in slots_for(s.trainer, new_start.date()):
        raise ApiError("That time slot is no longer available.", 409, "slot_taken")
    s.starts_at, s.ends_at = new_start, new_start + timedelta(minutes=s.trainer.session_minutes)
    s.slot_lock, s.reminded = f"{s.trainer_id}|{new_start.isoformat()}", False
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ApiError("That time slot is no longer available.", 409, "slot_taken")
    N.notify(s.member.user, "trainer_booking", "Session rescheduled", f"Now {fmt_dt(new_start)} with {s.trainer.name}.", "/member/bookings")


# ================================================================== attendance
def find_member_by_code(code):
    code = (code or "").strip()
    if code.upper().startswith("BM:"):
        code = code[3:]
    return (Member.query.filter(Member.qr_token == code).first()
            or Member.query.filter(func.upper(Member.member_code) == code.upper()).first())


def check_in(code, staff, force=False):
    member = find_member_by_code(code)
    if not member:
        raise ApiError("Member not found. Check the ID or QR code.", 404)
    if member.user.status != "active":
        raise ApiError(f"{member.user.full_name}'s account is suspended.", 403)
    method = "qr" if (code or "").upper().startswith("BM:") or len(code or "") > 20 else "member_id"
    now = datetime.now()
    last = Attendance.query.filter_by(member_id=member.id).order_by(Attendance.checked_in_at.desc()).first()
    cool = timedelta(minutes=current_app.config["CHECKIN_COOLDOWN_MIN"])
    if last and last.checked_out_at is None and now - last.checked_in_at < timedelta(hours=14):
        if now - last.checked_in_at < cool:
            raise ApiError(f"{member.user.first_name} already checked in at {last.checked_in_at:%I:%M %p}.", 409, "duplicate")
        last.checked_out_at = now
        db.session.commit()
        return {"action": "checked_out", "member": member.user.full_name, "at": now.strftime("%I:%M %p")}
    if last and last.checked_out_at and now - last.checked_out_at < cool:
        raise ApiError(f"{member.user.first_name} just checked out. Please wait a few minutes.", 409, "duplicate")
    m = active_membership(member)
    if not m and not force:
        raise ApiError(f"{member.user.full_name}: membership is not active. Ask them to renew at the front desk.", 402, "inactive")
    db.session.add(Attendance(member_id=member.id, checked_in_at=now, method=method, recorded_by=staff.id if staff else None))
    db.session.commit()
    return {"action": "checked_in", "member": member.user.full_name, "at": now.strftime("%I:%M %p"),
            "plan": m.plan.name if m else "No active plan", "code": member.member_code}


# =================================================================== progress
def week_start(d):
    return d - timedelta(days=d.weekday())


def weekly_attendance(member_id, weeks=8):
    start = week_start(date.today()) - timedelta(weeks=weeks - 1)
    rows = Attendance.query.filter(Attendance.member_id == member_id,
                                   Attendance.checked_in_at >= datetime.combine(start, time.min)).all()
    counts = {start + timedelta(weeks=i): 0 for i in range(weeks)}
    for r in rows:
        k = week_start(r.checked_in_at.date())
        if k in counts:
            counts[k] += 1
    return [(k.strftime("%d %b"), v) for k, v in sorted(counts.items())]


def current_streak(member_id):
    """Consecutive weeks (ending this week or last) with at least one visit."""
    days = {r.checked_in_at.date() for r in Attendance.query.filter_by(member_id=member_id).all()}
    weeks = {week_start(d) for d in days}
    w, streak = week_start(date.today()), 0
    if w not in weeks:
        w -= timedelta(weeks=1)
    while w in weeks:
        streak += 1
        w -= timedelta(weeks=1)
    return streak


def progress_series(member_id, metric):
    rows = (FitnessProgress.query.filter_by(member_id=member_id, metric=metric)
            .order_by(FitnessProgress.recorded_on).all())
    return [{"date": r.recorded_on.strftime("%d %b"), "value": r.value} for r in rows]


def can_view_member(user, member):
    if user.role_name == "admin":
        return True
    if user.member and user.member.id == member.id:
        return True
    if user.role_name == "trainer" and user.trainer:
        t = user.trainer
        return member.assigned_trainer_id == t.id or TrainerSession.query.filter_by(trainer_id=t.id, member_id=member.id).first() is not None
    return False


# ================================================================ quiz engine
def recommend(goal, experience, days):
    best, best_score = None, -1
    for r in QuizRule.query.filter_by(active=True).all():
        if r.goal not in ("any", goal) or r.experience not in ("any", experience) or not (r.min_days <= days <= r.max_days):
            continue
        score = (r.priority * 10) + (4 if r.goal == goal else 0) + (2 if r.experience == experience else 0)
        if score > best_score:
            best, best_score = r, score
    if not best:
        return {"program": "Book a free trial and a coach will build a plan around you.", "plan": None, "classes": [], "trainer": None}
    trainer = None
    if best.trainer_specialization:
        trainer = (Trainer.query.filter(Trainer.active.is_(True), Trainer.specialization.ilike(f"%{best.trainer_specialization}%")).first())
    trainer = trainer or Trainer.query.filter_by(active=True).order_by(Trainer.sort).first()
    classes = []
    if best.class_category:
        classes = (GymClass.query.filter(GymClass.category == best.class_category, GymClass.status == "scheduled",
                                         GymClass.starts_at > datetime.now()).order_by(GymClass.starts_at).limit(3).all())
    return {"program": best.program, "plan": best.plan, "classes": classes, "trainer": trainer}


# ===================================================================== jobs
def run_jobs():
    """Idempotent scheduled work. Run hourly (cron / `flask jobs`)."""
    out = {"offers_expired": 0, "memberships_expired": 0, "reminders": 0, "holds_released": 0}
    now = datetime.now()
    # 1. Waitlist offers that were never claimed -> next person
    for b in ClassBooking.query.filter(ClassBooking.status == "offered", ClassBooking.offer_expires_at < now).all():
        b.status = "expired"
        _free_spot(b.class_id)
        db.session.commit()
        N.notify(b.member.user, "waitlist", "Your waitlist offer expired", f"The spot in {b.gym_class.name} went to the next person.", email=False)
        promote_waitlist(b.class_id)
        out["offers_expired"] += 1
    # 2. Unpaid trainer sessions hold a slot for 30 minutes
    for s in TrainerSession.query.filter(TrainerSession.status == "pending_payment",
                                         TrainerSession.created_at < now - timedelta(minutes=30)).all():
        s.status, s.slot_lock = "cancelled", None
        out["holds_released"] += 1
    db.session.commit()
    # 3. Membership expiry + reminders
    today = date.today()
    for m in Membership.query.filter(Membership.status == "active").all():
        left = (m.end_date - today).days
        sent = set(filter(None, (m.reminders_sent or "").split(",")))
        if left < 0:
            m.status = "expired"
            db.session.commit()
            out["memberships_expired"] += 1
            N.notify(m.member.user, "membership_expired", "Your membership has expired",
                     f"Your {m.plan.name} membership ended on {m.end_date:%d %b}. Renew to keep booking classes.", "/member/membership", cta="Renew now")
            if not active_membership(m.member) and not upcoming_membership(m.member):
                for b in ClassBooking.query.filter(ClassBooking.member_id == m.member_id, ClassBooking.status.in_(("booked", "offered", "waitlist"))).all():
                    if b.gym_class.starts_at > now:
                        cancel_class_booking(b, by_staff=False if b.status == "waitlist" else True)
            continue
        for mark in (30, 7, 1, 0):
            if left <= mark and str(mark) not in sent and not m.cancel_at_period_end:
                if mark == 0 and left != 0:
                    continue
                sent.add(str(mark))
                m.reminders_sent = ",".join(sorted(sent))
                db.session.commit()
                label = "today" if left == 0 else f"in {left} day{'s' if left != 1 else ''}"
                N.notify(m.member.user, "membership_expiring", f"Your membership expires {label}",
                         f"Renew your {m.plan.name} plan to keep your access.", "/member/membership", cta="Renew", sms=(mark <= 7))
                out["reminders"] += 1
                break
    # 4. Class + session reminders (next 24h)
    horizon = now + timedelta(hours=24)
    for b in (ClassBooking.query.join(GymClass).filter(ClassBooking.status == "booked", ClassBooking.reminded.is_(False),
                                                       GymClass.starts_at > now, GymClass.starts_at <= horizon).all()):
        b.reminded = True
        db.session.commit()
        N.notify(b.member.user, "class_reminder", "Class reminder", f"{b.gym_class.name} starts {fmt_dt(b.gym_class.starts_at)}.", "/member/bookings", sms=True)
        out["reminders"] += 1
    for s in TrainerSession.query.filter(TrainerSession.status == "confirmed", TrainerSession.reminded.is_(False),
                                         TrainerSession.starts_at > now, TrainerSession.starts_at <= horizon).all():
        s.reminded = True
        db.session.commit()
        N.notify(s.member.user, "session_reminder", "Training session reminder",
                 f"{s.service} with {s.trainer.name} — {fmt_dt(s.starts_at)}.", "/member/bookings", sms=True)
        out["reminders"] += 1
    return out
