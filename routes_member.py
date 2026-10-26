import io
from datetime import date, datetime, timedelta

import qrcode
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import services as S
from core import ApiError, current_user, login_required, require_member
from models import (Attendance, ClassBooking, FitnessGoal, FitnessProgress, GymClass, Membership, MembershipPlan,
                    Notification, Payment, Trainer, TrainerSession, db)

bp = Blueprint("member", __name__, url_prefix="/member")

MEMBER_NAV = [("home", "Home", "/member", "⌂"), ("classes", "Classes", "/member/classes", "◷"),
              ("bookings", "Bookings", "/member/bookings", "✓"), ("membership", "Membership", "/member/membership", "★"),
              ("profile", "Profile", "/account/", "☺")]
MEMBER_MORE = [("trainers", "Book a trainer", "/member/trainers"), ("payments", "Payments", "/member/payments"),
               ("progress", "Progress", "/member/progress"), ("qr", "Check-in QR", "/member/qr"),
               ("notifications", "Notifications", "/account/notifications")]


def ctx(active):
    return dict(nav=MEMBER_NAV, more=MEMBER_MORE, active=active, area="member")


@bp.route("/")
@login_required("member")
def home():
    m = require_member()
    mem = S.active_membership(m)
    now = datetime.now()
    classes = (ClassBooking.query.join(GymClass).filter(ClassBooking.member_id == m.id, ClassBooking.status.in_(("booked", "offered")),
                                                        GymClass.starts_at > now).order_by(GymClass.starts_at).limit(4).all())
    sessions = (TrainerSession.query.filter(TrainerSession.member_id == m.id, TrainerSession.status.in_(("confirmed", "pending_payment")),
                                            TrainerSession.starts_at > now).order_by(TrainerSession.starts_at).limit(3).all())
    history = Attendance.query.filter_by(member_id=m.id).order_by(Attendance.checked_in_at.desc()).limit(6).all()
    payments = Payment.query.filter_by(user_id=m.user_id).order_by(Payment.created_at.desc()).limit(4).all()
    notes = Notification.query.filter_by(user_id=m.user_id).order_by(Notification.created_at.desc()).limit(5).all()
    month_start = date.today().replace(day=1)
    visits = Attendance.query.filter(Attendance.member_id == m.id, Attendance.checked_in_at >= datetime.combine(month_start, datetime.min.time())).count()
    weight = FitnessProgress.query.filter_by(member_id=m.id, metric="weight").order_by(FitnessProgress.recorded_on.desc()).limit(2).all()
    return render_template("member/home.html", m=m, mem=mem, info=S.membership_info(mem), upcoming_mem=S.upcoming_membership(m),
                           classes=classes, sessions=sessions, history=history, payments=payments, notes=notes,
                           visits=visits, streak=S.current_streak(m.id), weekly=S.weekly_attendance(m.id, 8),
                           weight=weight, **ctx("home"))


@bp.route("/classes")
@login_required("member")
def classes():
    m = require_member()
    q = GymClass.query.filter(GymClass.status == "scheduled", GymClass.starts_at > datetime.now(),
                              GymClass.starts_at < datetime.now() + timedelta(days=21))
    items = q.order_by(GymClass.starts_at).all()
    mine = {b.class_id: b for b in ClassBooking.query.filter_by(member_id=m.id).all()}
    mem = S.active_membership(m)
    return render_template("member/classes.html", items=items, mine=mine, mem=mem,
                           categories=sorted({c.category for c in items}), **ctx("classes"))


@bp.route("/trainers")
@login_required("member")
def trainers():
    m = require_member()
    ts = Trainer.query.filter_by(active=True).order_by(Trainer.sort, Trainer.id).all()
    mem = S.active_membership(m)
    left = None
    if mem and mem.plan.pt_sessions:
        left = max(mem.plan.pt_sessions - S.sessions_included_used(m, datetime.now()), 0)
    return render_template("member/trainers.html", trainers=ts, selected=request.args.get("trainer", type=int), left=left,
                           mem=mem, **ctx("trainers"))


@bp.route("/bookings")
@login_required("member")
def bookings():
    m = require_member()
    now = datetime.now()
    cls = ClassBooking.query.join(GymClass).filter(ClassBooking.member_id == m.id).order_by(GymClass.starts_at.desc()).limit(60).all()
    ses = TrainerSession.query.filter_by(member_id=m.id).order_by(TrainerSession.starts_at.desc()).limit(40).all()
    return render_template("member/bookings.html", cls=cls, ses=ses, now=now, **ctx("bookings"))


@bp.route("/membership")
@login_required("member")
def membership():
    m = require_member()
    plans = MembershipPlan.query.filter_by(status="active").order_by(MembershipPlan.sort, MembershipPlan.price_monthly_cents).all()
    mem = S.active_membership(m)
    history = Membership.query.filter_by(member_id=m.id).order_by(Membership.created_at.desc()).limit(10).all()
    return render_template("member/membership.html", plans=plans, mem=mem, info=S.membership_info(mem),
                           upcoming_mem=S.upcoming_membership(m), history=history,
                           preselect=request.args.get("plan", type=int), **ctx("membership"))


@bp.route("/payments")
@login_required("member")
def payments():
    items = Payment.query.filter_by(user_id=current_user().id).order_by(Payment.created_at.desc()).limit(100).all()
    return render_template("member/payments.html", items=items, **ctx("payments"))


@bp.route("/payments/return")
@login_required("member")
def payment_return():
    """Where Stripe sends the browser back. We re-verify with Stripe; we never trust the URL alone."""
    pay = db.session.get(Payment, request.args.get("pid", type=int) or 0)
    if not pay or pay.user_id != current_user().id:
        abort(404)
    sid = request.args.get("session_id")
    if request.args.get("cancelled"):
        flash("Your payment could not be completed — no money was taken. You can try again below.", "error")
    elif sid and S.stripe_enabled() and pay.provider_ref == sid:
        try:
            s = S._stripe().checkout.Session.retrieve(sid)
            if s.payment_status == "paid":
                S.fulfill_payment(pay, s.payment_intent)
                flash("Payment received — thank you! Your receipt is ready.", "ok")
            else:
                flash("We're still waiting for your bank to confirm this payment.", "info")
        except Exception:
            flash("We couldn't confirm your payment yet. If you were charged it will appear here shortly.", "info")
    return redirect(url_for("member.payments"))


@bp.route("/payments/<int:pid>/receipt")
@login_required("member", "admin")
def receipt(pid):
    pay = db.session.get(Payment, pid)
    u = current_user()
    if not pay or (pay.user_id != u.id and u.role_name != "admin"):
        abort(404)
    return render_template("member/receipt.html", p=pay)


@bp.route("/progress")
@login_required("member")
def progress():
    m = require_member()
    entries = FitnessProgress.query.filter_by(member_id=m.id).order_by(FitnessProgress.recorded_on.desc(), FitnessProgress.id.desc()).limit(60).all()
    lifts = sorted({e.metric[3:] for e in entries if e.metric.startswith("pr:")})
    goals = FitnessGoal.query.filter_by(member_id=m.id).order_by(FitnessGoal.created_at.desc()).all()
    return render_template("member/progress.html", m=m, entries=entries, lifts=lifts, goals=goals,
                           streak=S.current_streak(m.id), weekly=S.weekly_attendance(m.id, 12),
                           visits=Attendance.query.filter_by(member_id=m.id).count(), **ctx("progress"))


@bp.route("/qr")
@login_required("member")
def qr():
    return render_template("member/qr.html", m=require_member(), **ctx("qr"))


@bp.route("/qr.png")
@login_required("member")
def qr_png():
    m = require_member()
    img = qrcode.make(f"BM:{m.qr_token}", box_size=9, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)
