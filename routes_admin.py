import math
import os
import secrets
from collections import Counter
from datetime import date, datetime, time, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

import notifications as N
import services as S
from core import ApiError, Form, current_user, login_required, make_token, ok, payload, save_upload
from models import (FAQ, AnalyticsEvent, Attendance, ClassBooking, ContactMessage, EmailLog, Facility, GymClass, Media,
                    Member, Membership, MembershipPlan, Payment, QuizRule, Role, SiteContent, Testimonial, Trainer,
                    TrainerAvailability, TrainerSession, TourBooking, TrialLead, User, db)

bp = Blueprint("admin", __name__, url_prefix="/admin")

NAV = [("dashboard", "Dashboard", "/admin", "▦"), ("members", "Members", "/admin/members", "☺"),
       ("bookings", "Bookings", "/admin/bookings", "✓"), ("payments", "Payments", "/admin/payments", "$"),
       ("attendance", "Attendance", "/admin/attendance", "◷")]
MORE = [("leads", "Trial leads", "/admin/bookings?type=trial"), ("tours", "Tours", "/admin/bookings?type=tour"),
        ("messages", "Messages", "/admin/messages"), ("plans", "Membership plans", "/admin/crud/plans"),
        ("classes", "Classes", "/admin/crud/classes"), ("trainers", "Trainers", "/admin/trainers"),
        ("testimonials", "Testimonials", "/admin/crud/testimonials"), ("facilities", "Facilities", "/admin/crud/facilities"),
        ("faqs", "FAQs", "/admin/crud/faqs"), ("quiz", "Goal quiz rules", "/admin/crud/quiz"),
        ("content", "Website content", "/admin/content"), ("media", "Media library", "/admin/media"),
        ("analytics", "Analytics", "/admin/analytics"), ("outbox", "Email log", "/admin/outbox"),
        ("staff", "Staff check-in", "/staff/checkin")]


def ctx(active):
    return dict(nav=NAV, more=MORE, active=active, area="admin")


@bp.before_request
def guard():
    u = current_user()
    if not u:
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
    if u.role_name != "admin":
        abort(403)


def paginate(q, per=15):
    page = max(request.args.get("page", 1, type=int), 1)
    total = q.count()
    pages = max(math.ceil(total / per), 1)
    page = min(page, pages)
    return q.offset((page - 1) * per).limit(per).all(), dict(page=page, pages=pages, total=total)


def qs(**over):
    args = {k: v for k, v in request.args.items() if k != "page"}
    args.update({k: v for k, v in over.items() if v is not None})
    return "?" + "&".join(f"{k}={v}" for k, v in args.items() if v not in ("", None))


# ================================================================ dashboard
def by_day(dts, days=14):
    start = date.today() - timedelta(days=days - 1)
    c = Counter(d.date() for d in dts if d and d.date() >= start)
    return [((start + timedelta(days=i)).strftime("%d %b"), c.get(start + timedelta(days=i), 0)) for i in range(days)]


def by_month(items, n=6):
    """items: [(datetime, value)]"""
    today = date.today().replace(day=1)
    months = []
    for i in range(n - 1, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        months.append((y, m))
    tot = {k: 0 for k in months}
    for d, v in items:
        if d and (d.year, d.month) in tot:
            tot[(d.year, d.month)] += v
    return [(date(y, m, 1).strftime("%b"), tot[(y, m)]) for (y, m) in months]


@bp.route("/")
def dashboard():
    today, now = date.today(), datetime.now()
    d0, d1 = datetime.combine(today, time.min), datetime.combine(today + timedelta(days=1), time.min)
    m0 = datetime.combine(today.replace(day=1), time.min)
    active_ids = {m.member_id for m in Membership.query.filter(Membership.status == "active", Membership.start_date <= today, Membership.end_date >= today)}
    paid = Payment.query.filter(Payment.status == "paid").all()
    pend = Payment.query.filter_by(status="pending").all()
    kpis = [
        ("Total members", Member.query.count(), "/admin/members"),
        ("Active members", len(active_ids), "/admin/members?f=active"),
        ("New members (30d)", User.query.join(Role).filter(Role.name == "member", User.created_at >= now - timedelta(days=30)).count(), "/admin/members"),
        ("Today's attendance", Attendance.query.filter(Attendance.checked_in_at >= d0, Attendance.checked_in_at < d1).count(), "/admin/attendance"),
        ("Today's bookings", ClassBooking.query.join(GymClass).filter(ClassBooking.status.in_(("booked", "attended")), GymClass.starts_at >= d0, GymClass.starts_at < d1).count()
         + TrainerSession.query.filter(TrainerSession.status == "confirmed", TrainerSession.starts_at >= d0, TrainerSession.starts_at < d1).count(), "/admin/bookings"),
        ("Revenue this month", "$" + f"{sum(p.amount_cents for p in paid if p.paid_at and p.paid_at >= m0) / 100:,.0f}", "/admin/payments?status=paid"),
        ("Pending payments", f"{len(pend)} · ${sum(p.amount_cents for p in pend) / 100:,.0f}", "/admin/payments?status=pending"),
        ("Free trial leads (new)", TrialLead.query.filter_by(status="NEW").count(), "/admin/bookings?type=trial"),
        ("Tour requests (pending)", TourBooking.query.filter_by(status="pending").count(), "/admin/bookings?type=tour"),
    ]
    charts = {
        "revenue": by_month([(p.paid_at, p.amount_cents / 100) for p in paid]),
        "growth": by_month([(u.created_at, 1) for u in User.query.join(Role).filter(Role.name == "member")]),
        "attendance": by_day([a.checked_in_at for a in Attendance.query.filter(Attendance.checked_in_at >= now - timedelta(days=14))]),
        "bookings": by_day([b.created_at for b in ClassBooking.query.filter(ClassBooking.created_at >= now - timedelta(days=14))]),
        "leads": by_day([l.created_at for l in TrialLead.query.filter(TrialLead.created_at >= now - timedelta(days=14))]),
    }
    expiring = (Membership.query.filter(Membership.status == "active", Membership.end_date >= today, Membership.end_date <= today + timedelta(days=7)).count())
    return render_template("admin/dashboard.html", kpis=kpis, charts=charts, expiring=expiring, **ctx("dashboard"))


# ================================================================= members
@bp.route("/members")
def members():
    q = Member.query.join(User)
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(db.or_(User.full_name.ilike(like), User.email.ilike(like), User.phone.ilike(like), Member.member_code.ilike(like)))
    f = request.args.get("f", "")
    today = date.today()
    if f == "suspended":
        q = q.filter(User.status == "suspended")
    elif f in ("active", "none", "expiring"):
        act = db.session.query(Membership.member_id).filter(Membership.status == "active", Membership.start_date <= today, Membership.end_date >= today)
        if f == "expiring":
            act = act.filter(Membership.end_date <= today + timedelta(days=7))
        q = q.filter(Member.id.in_(act)) if f != "none" else q.filter(~Member.id.in_(act))
    rows, pg = paginate(q.order_by(User.created_at.desc()))
    data = []
    for m in rows:
        mem = S.active_membership(m)
        data.append({"m": m, "plan": mem.plan.name if mem else None, "end": mem.end_date if mem else None})
    return render_template("admin/members.html", rows=data, pg=pg, term=term, f=f, qs=qs, **ctx("members"))


@bp.route("/members/<int:mid>", methods=["GET", "POST"])
def member(mid):
    m = db.session.get(Member, mid) or abort(404)
    if request.method == "POST":
        f = Form(request.form)
        f.text("full_name", "Name", max=100, min=2)
        f.email()
        f.phone(required=False)
        f.text("emergency_contact", "Emergency contact", required=False, max=160)
        if f.ok and f.v["email"] != m.user.email and User.query.filter_by(email=f.v["email"]).first():
            f.errors["email"] = "That email is already used by another account."
        if f.ok:
            m.user.full_name, m.user.email, m.user.phone = f.v["full_name"], f.v["email"], f.v["phone"] or None
            m.emergency_contact = f.v["emergency_contact"] or None
            tid = request.form.get("assigned_trainer_id", type=int)
            m.assigned_trainer_id = tid or None
            db.session.commit()
            flash("Member updated.", "ok")
        else:
            flash(next(iter(f.errors.values())), "error")
        return redirect(url_for("admin.member", mid=mid))
    mem = S.active_membership(m)
    return render_template("admin/member.html", m=m, mem=mem, plans=MembershipPlan.query.filter(MembershipPlan.status != "archived").all(),
                           trainers=Trainer.query.filter_by(active=True).all(),
                           history=Membership.query.filter_by(member_id=m.id).order_by(Membership.created_at.desc()).all(),
                           payments=Payment.query.filter_by(user_id=m.user_id).order_by(Payment.created_at.desc()).limit(30).all(),
                           attendance=Attendance.query.filter_by(member_id=m.id).order_by(Attendance.checked_in_at.desc()).limit(30).all(),
                           cbs=ClassBooking.query.join(GymClass).filter(ClassBooking.member_id == m.id).order_by(GymClass.starts_at.desc()).limit(20).all(),
                           ses=TrainerSession.query.filter_by(member_id=m.id).order_by(TrainerSession.starts_at.desc()).limit(20).all(),
                           streak=S.current_streak(m.id), **ctx("members"))


# ================================================================ bookings
BADGE = {"booked": "ok", "confirmed": "ok", "approved": "ok", "paid": "ok", "attended": "ok", "completed": "ok", "CONVERTED": "ok",
         "pending": "warn", "pending_payment": "warn", "waitlist": "warn", "offered": "warn", "NEW": "warn", "requested": "warn",
         "cancelled": "bad", "failed": "bad", "no_show": "bad", "expired": "bad", "refunded": "bad", "NOT INTERESTED": "bad"}


def cell(text, status=False, href=None):
    return {"text": str(text), "badge": BADGE.get(str(text), "muted") if status else None, "href": href}


def act(label, api, body=None, confirm=None, prompt=None, style=""):
    return dict(label=label, api=api, body=body or {}, confirm=confirm, prompt=prompt, style=style)


TRIAL_STATUSES = ["NEW", "CONTACTED", "TRIAL BOOKED", "TRIAL COMPLETED", "CONVERTED", "NOT INTERESTED"]


@bp.route("/bookings")
def bookings():
    typ = request.args.get("type", "class")
    status, term = request.args.get("status", ""), (request.args.get("q") or "").strip()
    day = request.args.get("date", "")
    tabs = [(l, f"/admin/bookings?type={k}", typ == k) for k, l in (("class", "Class bookings"), ("session", "Trainer sessions"), ("trial", "Free trials"), ("tour", "Gym tours"))]
    filters, cols, rows = [], [], []
    trainer_opts = [("", "All trainers")] + [(str(t.id), t.name) for t in Trainer.query.all()]
    day_f = {"name": "date", "label": "Date", "type": "date", "value": day}
    q_f = {"name": "q", "label": "Search name/email", "type": "text", "value": term}
    dayrange = None
    if day:
        try:
            d = datetime.strptime(day, "%Y-%m-%d")
            dayrange = (d, d + timedelta(days=1))
        except ValueError:
            pass

    if typ == "class":
        q = ClassBooking.query.join(GymClass).join(Member).join(User, Member.user_id == User.id)
        if status:
            q = q.filter(ClassBooking.status == status)
        if request.args.get("trainer"):
            q = q.filter(GymClass.trainer_id == request.args.get("trainer", type=int))
        if request.args.get("class_name"):
            q = q.filter(GymClass.name.ilike(f"%{request.args['class_name']}%"))
        if term:
            q = q.filter(db.or_(User.full_name.ilike(f"%{term}%"), User.email.ilike(f"%{term}%")))
        if dayrange:
            q = q.filter(GymClass.starts_at >= dayrange[0], GymClass.starts_at < dayrange[1])
        items, pg = paginate(q.order_by(GymClass.starts_at.desc()))
        filters = [day_f, q_f, {"name": "class_name", "label": "Class", "type": "text", "value": request.args.get("class_name", "")},
                   {"name": "trainer", "label": "Trainer", "type": "select", "options": trainer_opts, "value": request.args.get("trainer", "")},
                   {"name": "status", "label": "Status", "type": "select", "value": status,
                    "options": [("", "Any")] + [(s, s) for s in ("booked", "waitlist", "offered", "attended", "no_show", "cancelled", "expired")]}]
        cols = ["Class", "When", "Member", "Trainer", "Status"]
        for b in items:
            a = []
            if b.status in ("booked", "offered", "waitlist"):
                a.append(act("Cancel", f"/api/class-bookings/{b.id}/status", {"status": "cancelled"}, "Cancel this booking and notify the member?", style="danger"))
            if b.status == "booked":
                a.append(act("Attended", f"/api/class-bookings/{b.id}/status", {"status": "attended"}))
            rows.append({"cells": [b.gym_class.name, b.gym_class.starts_at.strftime("%d %b %H:%M"),
                                   cell(b.member.user.full_name, href=f"/admin/members/{b.member_id}"),
                                   b.gym_class.trainer.name if b.gym_class.trainer else "—", cell(b.status, True)], "actions": a})
    elif typ == "session":
        q = TrainerSession.query.join(Member).join(User, Member.user_id == User.id)
        if status:
            q = q.filter(TrainerSession.status == status)
        if request.args.get("trainer"):
            q = q.filter(TrainerSession.trainer_id == request.args.get("trainer", type=int))
        if term:
            q = q.filter(db.or_(User.full_name.ilike(f"%{term}%"), User.email.ilike(f"%{term}%")))
        if dayrange:
            q = q.filter(TrainerSession.starts_at >= dayrange[0], TrainerSession.starts_at < dayrange[1])
        items, pg = paginate(q.order_by(TrainerSession.starts_at.desc()))
        filters = [day_f, q_f, {"name": "trainer", "label": "Trainer", "type": "select", "options": trainer_opts, "value": request.args.get("trainer", "")},
                   {"name": "status", "label": "Status", "type": "select", "value": status,
                    "options": [("", "Any")] + [(s, s) for s in ("confirmed", "pending_payment", "completed", "cancelled", "no_show")]}]
        cols = ["Service", "When", "Member", "Trainer", "Price", "Status"]
        for s in items:
            a = []
            if s.status in ("confirmed", "pending_payment"):
                a += [act("Complete", f"/api/sessions/{s.id}/status", {"status": "completed"}),
                      act("Reschedule", f"/admin/action/session/{s.id}/reschedule", prompt=[{"name": "start", "label": "New start", "type": "datetime-local"}]),
                      act("Cancel", f"/api/sessions/{s.id}/status", {"status": "cancelled"}, "Cancel this session?", style="danger")]
            if s.status == "pending_payment":
                a.insert(0, act("Approve", f"/admin/action/session/{s.id}/approve"))
            rows.append({"cells": [s.service, s.starts_at.strftime("%d %b %H:%M"), cell(s.member.user.full_name, href=f"/admin/members/{s.member_id}"),
                                   s.trainer.name, "Included" if s.included else f"${s.price_cents / 100:.0f}", cell(s.status, True)], "actions": a})
    elif typ == "trial":
        q = TrialLead.query
        if status:
            q = q.filter(TrialLead.status == status)
        if term:
            q = q.filter(db.or_(TrialLead.name.ilike(f"%{term}%"), TrialLead.email.ilike(f"%{term}%")))
        if dayrange:
            q = q.filter(TrialLead.preferred_date == dayrange[0].date())
        items, pg = paginate(q.order_by(TrialLead.created_at.desc()))
        filters = [day_f, q_f, {"name": "status", "label": "Status", "type": "select", "value": status, "options": [("", "Any")] + [(s, s) for s in TRIAL_STATUSES]}]
        cols = ["Lead", "Contact", "Goal / level", "Preferred", "Status"]
        for l in items:
            nxt = [s for s in TRIAL_STATUSES if s != l.status]
            rows.append({"cells": [cell(l.name), f"{l.email} · {l.phone}", f"{l.goal} · {l.experience} · age {l.age}",
                                   f"{l.preferred_date:%d %b} {l.preferred_time}", cell(l.status, True)],
                         "actions": [act(s.title(), f"/admin/action/trial/{l.id}/status", {"status": s}) for s in nxt[:3]] +
                                    [dict(label="Email", href=f"mailto:{l.email}")]})
    elif typ == "tour":
        q = TourBooking.query
        if status:
            q = q.filter(TourBooking.status == status)
        if term:
            q = q.filter(db.or_(TourBooking.name.ilike(f"%{term}%"), TourBooking.email.ilike(f"%{term}%")))
        if dayrange:
            q = q.filter(TourBooking.tour_date == dayrange[0].date())
        items, pg = paginate(q.order_by(TourBooking.tour_date.desc(), TourBooking.tour_time))
        filters = [day_f, q_f, {"name": "status", "label": "Status", "type": "select", "value": status,
                                "options": [("", "Any")] + [(s, s) for s in ("pending", "approved", "cancelled", "completed")]}]
        cols = ["Visitor", "Contact", "When", "Status"]
        for t in items:
            a = []
            if t.status in ("pending", "approved"):
                if t.status == "pending":
                    a.append(act("Approve", f"/admin/action/tour/{t.id}/approve"))
                a += [act("Reschedule", f"/admin/action/tour/{t.id}/reschedule", prompt=[{"name": "tour_date", "label": "New date", "type": "date"},
                                                                                      {"name": "tour_time", "label": "New time", "type": "time"}]),
                      act("Complete", f"/admin/action/tour/{t.id}/complete"),
                      act("Cancel", f"/admin/action/tour/{t.id}/cancel", confirm="Cancel this tour and email the visitor?", style="danger")]
            a.append(dict(label="Email", href=f"mailto:{t.email}"))
            rows.append({"cells": [cell(t.name), f"{t.email} · {t.phone}", f"{t.tour_date:%a %d %b} at {t.tour_time}", cell(t.status, True)], "actions": a})
    else:
        abort(404)
    return render_template("admin/list.html", title="Bookings", tabs=tabs, filters=filters, cols=cols, rows=rows, pg=pg, qs=qs,
                           empty="Nothing matches these filters.", **ctx("bookings"))


@bp.route("/messages")
def messages():
    status = request.args.get("status", "")
    q = ContactMessage.query
    if status:
        q = q.filter_by(status=status)
    items, pg = paginate(q.order_by(ContactMessage.created_at.desc()))
    rows = []
    for m in items:
        a = [act(s.title(), f"/admin/action/message/{m.id}/status", {"status": s}) for s in ("READ", "RESPONDED", "CLOSED") if s != m.status]
        rows.append({"cells": [cell(m.name), f"{m.email} {m.phone or ''}", m.message[:220], m.created_at.strftime("%d %b %H:%M"), cell(m.status, True)],
                     "actions": a + [dict(label="Reply", href=f"mailto:{m.email}?subject=Re: your message to {current_app.config['GYM_NAME']}")]})
    return render_template("admin/list.html", title="Contact messages", tabs=[], cols=["From", "Contact", "Message", "Received", "Status"], rows=rows, pg=pg, qs=qs,
                           filters=[{"name": "status", "label": "Status", "type": "select", "value": status, "options": [("", "Any")] + [(s, s) for s in ("NEW", "READ", "RESPONDED", "CLOSED")]}],
                           empty="No messages yet.", **ctx("messages"))


@bp.route("/payments")
def payments():
    status = request.args.get("status", "")
    q = Payment.query.join(User)
    if status:
        q = q.filter(Payment.status == status)
    if request.args.get("q"):
        q = q.filter(db.or_(User.full_name.ilike(f"%{request.args['q']}%"), User.email.ilike(f"%{request.args['q']}%"), Payment.transaction_id.ilike(f"%{request.args['q']}%")))
    items, pg = paginate(q.order_by(Payment.created_at.desc()))
    rows = []
    for p in items:
        a = []
        if p.status in ("pending", "failed"):
            a.append(act("Mark paid", f"/admin/action/payment/{p.id}/paid", confirm="Confirm you received this payment?"))
        if p.status == "paid":
            a.append(act("Refund", f"/admin/action/payment/{p.id}/refund", confirm="Refund this payment?", style="danger"))
            a.append(dict(label="Receipt", href=f"/member/payments/{p.id}/receipt"))
        rows.append({"cells": [cell(p.user.full_name, href=f"/admin/members/{p.user.member.id}" if p.user.member else None), p.description,
                               f"${p.amount_cents / 100:,.2f}", f"{p.provider} · {p.transaction_id or '—'}", p.created_at.strftime("%d %b %H:%M"),
                               cell(p.status + (" (refund requested)" if p.refund_status == "requested" else ""), True)], "actions": a})
    return render_template("admin/list.html", title="Payments", tabs=[], cols=["Member", "For", "Amount", "Provider / ref", "Created", "Status"], rows=rows, pg=pg, qs=qs,
                           filters=[{"name": "q", "label": "Search", "type": "text", "value": request.args.get("q", "")},
                                    {"name": "status", "label": "Status", "type": "select", "value": status, "options": [("", "Any")] + [(s, s) for s in ("pending", "paid", "failed", "refunded")]}],
                           empty="No payments yet.", **ctx("payments"))


@bp.route("/attendance")
def attendance():
    today, now = date.today(), datetime.now()
    d0 = datetime.combine(today, time.min)
    todays = Attendance.query.filter(Attendance.checked_in_at >= d0).order_by(Attendance.checked_in_at.desc()).all()
    recent = Attendance.query.filter(Attendance.checked_in_at >= now - timedelta(days=180)).all()
    daily = by_day([a.checked_in_at for a in recent], 14)
    weeks = Counter(S.week_start(a.checked_in_at.date()) for a in recent)
    weekly = [((S.week_start(today) - timedelta(weeks=i)).strftime("%d %b"), weeks.get(S.week_start(today) - timedelta(weeks=i), 0)) for i in range(7, -1, -1)]
    monthly = by_month([(a.checked_in_at, 1) for a in recent], 6)
    return render_template("admin/attendance.html", todays=todays, daily=daily, weekly=weekly, monthly=monthly, **ctx("attendance"))


@bp.route("/analytics")
def analytics():
    since = datetime.now() - timedelta(days=30)
    c = Counter(e.name for e in AnalyticsEvent.query.filter(AnalyticsEvent.created_at >= since))
    views = c.get("page_view", 0)

    def pct(a, b):
        return f"{(a / b * 100):.1f}%" if b else "—"
    leads = TrialLead.query.filter(TrialLead.created_at >= since)
    converted = leads.filter_by(status="CONVERTED").count()
    funnel = [("Website visits", views), ("Trial requests", c.get("trial_submit", 0)), ("Tour requests", c.get("tour_booking", 0)),
              ("Membership purchases", c.get("membership_purchase", 0)), ("Class bookings", c.get("class_booking", 0)), ("Trainer bookings", c.get("trainer_booking", 0))]
    rates = [("Visit → trial", pct(c.get("trial_submit", 0), views)), ("Visit → tour", pct(c.get("tour_booking", 0), views)),
             ("Trial → converted member", pct(converted, leads.count())), ("Visit → purchase", pct(c.get("membership_purchase", 0), views))]
    views_day = by_day([e.created_at for e in AnalyticsEvent.query.filter(AnalyticsEvent.name == "page_view", AnalyticsEvent.created_at >= since)], 14)
    return render_template("admin/analytics.html", funnel=funnel, rates=rates, views_day=views_day, **ctx("analytics"))


@bp.route("/outbox")
def outbox():
    items, pg = paginate(EmailLog.query.order_by(EmailLog.created_at.desc()))
    return render_template("admin/outbox.html", items=items, pg=pg, qs=qs, **ctx("outbox"))


# ============================================================ action dispatcher
def _tour_mail(t, title, line):
    N.send_email(t.email, title, N.email_html(title, [line]))


def do_action(kind, oid, action, d):
    if kind == "trial":
        l = db.session.get(TrialLead, oid) or abort(404)
        if action == "status" and d.get("status") in TRIAL_STATUSES:
            l.status = d["status"]
            db.session.commit()
            return "Lead updated."
    elif kind == "tour":
        t = db.session.get(TourBooking, oid) or abort(404)
        if action == "approve":
            t.status = "approved"
            db.session.commit()
            N.send_email(t.email, "Your tour is confirmed", N.email_html("Tour confirmed", [f"See you {t.tour_date:%A %d %B} at {t.tour_time}. Ask for the front desk when you arrive."]))
            return "Tour approved and visitor emailed."
        if action == "cancel":
            t.status = "cancelled"
            db.session.commit()
            _tour_mail(t, "Your tour was cancelled", "Sorry — we had to cancel your tour. Please book another time on our website.")
            return "Tour cancelled."
        if action == "complete":
            t.status = "completed"
            db.session.commit()
            return "Marked completed."
        if action == "reschedule":
            f = Form(d)
            f.date("tour_date", "Date", future=True)
            f.text("tour_time", "Time", max=5)
            f.check()
            t.tour_date, t.tour_time, t.status = f.v["tour_date"], f.v["tour_time"], "approved"
            db.session.commit()
            _tour_mail(t, "Your tour was rescheduled", f"Your new time: {t.tour_date:%A %d %B} at {t.tour_time}.")
            return "Tour rescheduled and visitor emailed."
    elif kind == "message":
        m = db.session.get(ContactMessage, oid) or abort(404)
        if action == "status" and d.get("status") in ("NEW", "READ", "RESPONDED", "CLOSED"):
            m.status = d["status"]
            db.session.commit()
            return "Message updated."
    elif kind == "session":
        s = db.session.get(TrainerSession, oid) or abort(404)
        if action == "approve" and s.status == "pending_payment":
            s.status = "confirmed"
            db.session.commit()
            N.notify(s.member.user, "trainer_booking", "Training session confirmed", f"{s.service} on {S.fmt_dt(s.starts_at)}.", "/member/bookings")
            return "Session approved."
        if action == "reschedule":
            try:
                new = datetime.fromisoformat(d.get("start", ""))
            except ValueError:
                raise ApiError("Choose a valid date and time.", 422)
            S.reschedule_session(s, new)
            return "Session rescheduled."
    elif kind == "payment":
        p = db.session.get(Payment, oid) or abort(404)
        if action == "paid":
            S.fulfill_payment(p, p.transaction_id)
            return "Payment marked as paid and the customer was notified."
        if action == "refund":
            S.process_refund(p)
            return "Refund processed."
    elif kind == "member":
        m = db.session.get(Member, oid) or abort(404)
        if action in ("suspend", "activate"):
            m.user.status = "suspended" if action == "suspend" else "active"
            db.session.commit()
            return f"Account {'suspended' if action == 'suspend' else 'activated'}."
        if action == "grant":
            plan = db.session.get(MembershipPlan, int(d.get("plan_id") or 0)) or abort(404)
            mem, pay = S.create_membership_purchase(m, plan, d.get("billing", "monthly"))
            pay.provider, pay.transaction_id = "manual", f"FRONTDESK-{pay.id}"
            S.fulfill_payment(pay, pay.transaction_id)
            return f"{plan.name} membership activated."
    elif kind == "class":
        c = db.session.get(GymClass, oid) or abort(404)
        if action == "cancel":
            S.cancel_class(c)
            return "Class cancelled and members notified."
    raise ApiError("Unknown action.", 400)


@bp.route("/action/<kind>/<int:oid>/<action>", methods=["POST"])
def action(kind, oid, action):
    return ok(message=do_action(kind, oid, action, payload()))


# ===================================================== generic content CRUD
def _trainer_opts():
    return [("", "— none —")] + [(str(t.id), t.name) for t in Trainer.query.order_by(Trainer.id)]


def _plan_opts():
    return [("", "— none —")] + [(str(p.id), p.name) for p in MembershipPlan.query.all()]


def crud_cfg(key):
    return {
        "plans": dict(model=MembershipPlan, title="Membership plans", singular="plan", order=MembershipPlan.sort, list=["name", "price_monthly_cents", "price_yearly_cents", "status"],
                      fields=[("name", "Name", "text", True), ("description", "Description", "textarea", False),
                              ("price_monthly_cents", "Monthly price", "money", True), ("price_yearly_cents", "Yearly price", "money", True),
                              ("duration_days", "Billing period (days, monthly)", "int", True), ("gym_access", "Gym access", "text", False),
                              ("classes_included", "Classes per month (-1 = unlimited)", "int", True), ("pt_sessions", "Personal training sessions / month", "int", True),
                              ("guest_passes", "Guest passes / month", "int", True), ("recovery_access", "Recovery access", "bool", False),
                              ("benefits", "Benefits (one per line)", "textarea", False), ("status", "Status", ("active", "draft", "archived"), True),
                              ("featured", "Highlight as most popular", "bool", False), ("sort", "Sort order", "int", False)]),
        "classes": dict(model=GymClass, title="Classes", singular="class", order=GymClass.starts_at.desc(), list=["name", "category", "starts_at", "capacity", "status"],
                        fields=[("name", "Class name", "text", True), ("category", "Type", ("Strength", "HIIT", "Yoga", "Boxing", "CrossFit", "Cardio", "Mobility", "Functional Training"), True),
                                ("description", "Description", "textarea", False), ("trainer_id", "Trainer", _trainer_opts, False),
                                ("starts_at", "Start (date & time)", "datetime", True), ("duration", "Duration (minutes)", "virtual_int", True),
                                ("capacity", "Capacity", "int", True), ("difficulty", "Difficulty (1-5)", "int", True), ("location", "Location", "text", False),
                                ("image", "Image", "image", False), ("status", "Status", ("scheduled", "cancelled", "completed"), True)]),
        "testimonials": dict(model=Testimonial, title="Testimonials", singular="testimonial", order=Testimonial.created_at.desc(), list=["name", "rating", "published"],
                             fields=[("name", "Member name", "text", True), ("photo", "Photo", "image", False), ("rating", "Rating (1-5)", "int", True),
                                     ("text", "Testimonial", "textarea", True), ("goal", "Goal", "text", False), ("duration", "Training duration", "text", False),
                                     ("result", "Result", "text", False), ("published", "Published on website", "bool", False)]),
        "facilities": dict(model=Facility, title="Facilities", singular="facility", order=Facility.sort, list=["name", "published"],
                           fields=[("name", "Name", "text", True), ("description", "Description", "textarea", False), ("image", "Image", "image", False),
                                   ("pos_x", "Tour hotspot X % (0-100)", "int", False), ("pos_y", "Tour hotspot Y % (0-100)", "int", False),
                                   ("sort", "Sort order", "int", False), ("published", "Published", "bool", False)]),
        "faqs": dict(model=FAQ, title="FAQs", singular="FAQ", order=FAQ.sort, list=["question", "published"],
                     fields=[("question", "Question", "text", True), ("answer", "Answer", "textarea", True), ("sort", "Sort order", "int", False), ("published", "Published", "bool", False)]),
        "quiz": dict(model=QuizRule, title="Goal quiz rules", singular="rule", order=QuizRule.priority.desc(), list=["goal", "experience", "min_days", "max_days", "active"],
                     fields=[("goal", "Goal", ("any", "Build muscle", "Lose weight", "Improve strength", "Improve endurance", "Improve mobility", "General fitness"), True),
                             ("experience", "Experience", ("any", "beginner", "intermediate", "advanced"), True), ("min_days", "Min days/week", "int", True),
                             ("max_days", "Max days/week", "int", True), ("plan_id", "Recommended plan", _plan_opts, False),
                             ("class_category", "Recommended class type", ("", "Strength", "HIIT", "Yoga", "Boxing", "CrossFit", "Cardio", "Mobility", "Functional Training"), False),
                             ("trainer_specialization", "Trainer specialization keyword", "text", False), ("program", "Program recommendation text", "textarea", True),
                             ("priority", "Priority (higher wins)", "int", False), ("active", "Active", "bool", False)]),
    }[key]


def _opts(t):
    opts = t() if callable(t) else t
    return [(o, o) if isinstance(o, str) else o for o in opts]


def _parse_field(obj, name, label, typ, req, form, files, user):
    """Returns (value, error) for one field."""
    raw = (form.get(name) or "").strip() if typ not in ("bool", "image") else None
    if typ == "bool":
        return form.get(name) == "on", None
    if typ == "image":
        f = files.get(name)
        if f and f.filename:
            return save_upload(f, "image", user), None
        return getattr(obj, name, None) if obj else None, None
    if raw == "":
        return (None, f"{label} is required.") if req else (None, None)
    try:
        if typ in ("int", "virtual_int"):
            return int(raw), None
        if typ == "money":
            return int(round(float(raw) * 100)), None
        if typ == "datetime":
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M"), None
    except ValueError:
        return None, f"{label} is not valid."
    if isinstance(typ, tuple) or callable(typ):
        valid = {v for v, _ in _opts(typ)}
        if raw not in valid:
            return None, f"{label}: choose a valid option."
        return (int(raw) if name.endswith("_id") and raw else (raw or None)), None
    if len(raw) > 5000:
        return None, f"{label} is too long."
    return raw, None


@bp.route("/crud/<key>")
def crud_list(key):
    try:
        cfg = crud_cfg(key)
    except KeyError:
        abort(404)
    items, pg = paginate(cfg["model"].query.order_by(cfg["order"]), 20)
    return render_template("admin/crud_list.html", cfg=cfg, key=key, items=items, pg=pg, qs=qs, **ctx(key if key in dict((m[0], 1) for m in MORE) else key))


@bp.route("/crud/<key>/new", methods=["GET", "POST"])
@bp.route("/crud/<key>/<int:oid>", methods=["GET", "POST"])
def crud_edit(key, oid=None):
    try:
        cfg = crud_cfg(key)
    except KeyError:
        abort(404)
    obj = (db.session.get(cfg["model"], oid) or abort(404)) if oid else None
    errors = {}
    if request.method == "POST":
        new = obj is None
        target = cfg["model"]() if new else obj
        vals, virtual = {}, {}
        for name, label, typ, req in cfg["fields"]:
            v, err = _parse_field(target if not new else None, name, label, typ, req, request.form, request.files, current_user())
            if err:
                errors[name] = err
            elif typ == "virtual_int":
                virtual[name] = v
            else:
                vals[name] = v
        if key == "classes" and not errors:
            if virtual["duration"] < 10 or virtual["duration"] > 480:
                errors["duration"] = "Duration must be 10-480 minutes."
            if not 1 <= vals["difficulty"] <= 5:
                errors["difficulty"] = "Difficulty must be 1-5."
            if vals["capacity"] < 1:
                errors["capacity"] = "Capacity must be at least 1."
            if not new and vals["capacity"] < target.booked:
                errors["capacity"] = f"{target.booked} people are already booked — capacity can't go below that."
        if key == "testimonials" and not errors and not 1 <= vals["rating"] <= 5:
            errors["rating"] = "Rating must be 1-5."
        if key == "plans" and not errors and (vals["price_monthly_cents"] < 0 or vals["price_yearly_cents"] < 0):
            errors["price_monthly_cents"] = "Prices can't be negative."
        if not errors:
            for k, v in vals.items():
                setattr(target, k, v)
            if key == "classes":
                target.ends_at = target.starts_at + timedelta(minutes=virtual["duration"])
            if new:
                db.session.add(target)
            db.session.commit()
            flash(f"{cfg['singular'].title()} saved.", "ok")
            return redirect(url_for("admin.crud_list", key=key))
        flash("Please fix the highlighted fields.", "error")
    form_vals = {}
    src = obj
    for name, label, typ, req in cfg["fields"]:
        if request.method == "POST":
            form_vals[name] = request.form.get(name, "")
        elif src is not None:
            if typ == "virtual_int":
                form_vals[name] = src.duration_minutes
            elif typ == "money":
                form_vals[name] = f"{getattr(src, name) / 100:.2f}"
            elif typ == "datetime":
                form_vals[name] = getattr(src, name).strftime("%Y-%m-%dT%H:%M")
            else:
                form_vals[name] = "" if getattr(src, name) is None else getattr(src, name)
        else:
            form_vals[name] = {"capacity": 20, "difficulty": 3, "rating": 5, "min_days": 1, "max_days": 7, "sort": 0, "priority": 0, "duration": 60,
                               "duration_days": 30, "classes_included": -1, "pt_sessions": 0, "guest_passes": 0, "status": "active", "published": True, "active": True,
                               "location": "Main Floor"}.get(name, "")
    fields = [(n, l, (t if not callable(t) else "select"), r, _opts(t) if (isinstance(t, tuple) or callable(t)) else None) for n, l, t, r in cfg["fields"]]
    return render_template("admin/crud_form.html", cfg=cfg, key=key, obj=obj, fields=fields, vals=form_vals, errors=errors, **ctx(key))


@bp.route("/crud/<key>/<int:oid>/delete", methods=["POST"])
def crud_delete(key, oid):
    cfg = crud_cfg(key)
    obj = db.session.get(cfg["model"], oid) or abort(404)
    try:
        db.session.delete(obj)
        db.session.commit()
        flash("Deleted.", "ok")
    except IntegrityError:
        db.session.rollback()
        flash("This item is in use (members or bookings reference it). Set its status to archived/cancelled instead of deleting.", "error")
    return redirect(url_for("admin.crud_list", key=key))


@bp.route("/crud/classes/<int:oid>/cancel", methods=["POST"])
def class_cancel(oid):
    S.cancel_class(db.session.get(GymClass, oid) or abort(404))
    flash("Class cancelled and booked members were notified.", "ok")
    return redirect(url_for("admin.crud_list", key="classes"))


# =============================================================== trainers
@bp.route("/trainers")
def trainers():
    return render_template("admin/trainers.html", items=Trainer.query.order_by(Trainer.sort, Trainer.id).all(), **ctx("trainers"))


@bp.route("/trainers/new", methods=["GET", "POST"])
@bp.route("/trainers/<int:tid>", methods=["GET", "POST"])
def trainer_edit(tid=None):
    t = (db.session.get(Trainer, tid) or abort(404)) if tid else None
    errors = {}
    if request.method == "POST":
        f = Form(request.form)
        f.text("full_name", "Name", max=100, min=2)
        f.email()
        f.phone(required=False)
        f.text("specialization", "Specialization", max=160)
        f.text("bio", "Bio", required=False, max=2000)
        f.int_("experience_years", "Experience (years)", min=0, max=60)
        f.text("certifications", "Certifications", required=False, max=1000)
        f.int_("session_minutes", "Session length", min=15, max=180)
        f.float_("session_price", "Session price", min=0, max=5000)
        email_owner = User.query.filter_by(email=f.v["email"]).first() if f.v.get("email") else None
        if email_owner and (not t or email_owner.id != t.user_id):
            f.errors["email"] = "That email already belongs to another account."
        if f.ok:
            newuser = t is None
            if newuser:
                role = Role.query.filter_by(name="trainer").first()
                u = User(email=f.v["email"], password_hash=generate_password_hash(secrets.token_urlsafe(24)), role_id=role.id,
                         full_name=f.v["full_name"], phone=f.v["phone"] or None, email_verified=True)
                db.session.add(u)
                db.session.flush()
                t = Trainer(user_id=u.id, specialization=f.v["specialization"])
                db.session.add(t)
            else:
                u = t.user
                u.full_name, u.email, u.phone = f.v["full_name"], f.v["email"], f.v["phone"] or None
            t.specialization, t.bio, t.experience_years = f.v["specialization"], f.v["bio"], f.v["experience_years"]
            t.certifications, t.session_minutes, t.session_price_cents = f.v["certifications"], f.v["session_minutes"], int(round(f.v["session_price"] * 100))
            t.active = request.form.get("active") == "on"
            photo = request.files.get("photo")
            if photo and photo.filename:
                t.photo = save_upload(photo, "image", current_user())
            db.session.commit()
            if newuser:
                tok = make_token("reset", [u.id, u.password_hash[-12:]])
                N.send_email(u.email, "Your trainer account", N.email_html(f"Welcome to the team, {u.first_name}",
                             ["Your trainer account is ready. Choose your password to sign in and manage your schedule."], "Set my password",
                             current_app.config["BASE_URL"] + url_for("auth.reset", token=tok)))
                flash("Trainer created. An email with a set-password link was sent to them.", "ok")
            else:
                flash("Trainer saved.", "ok")
            return redirect(url_for("admin.trainer_edit", tid=t.id))
        errors = f.errors
        flash("Please fix the highlighted fields.", "error")
    return render_template("admin/trainer_form.html", t=t, errors=errors, form=request.form if request.method == "POST" else {},
                           days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], **ctx("trainers"))


@bp.route("/trainers/<int:tid>/availability", methods=["POST"])
def trainer_avail(tid):
    t = db.session.get(Trainer, tid) or abort(404)
    if request.form.get("delete"):
        a = db.session.get(TrainerAvailability, request.form.get("delete", type=int))
        if a and a.trainer_id == t.id:
            db.session.delete(a)
            db.session.commit()
    else:
        try:
            s, e = (datetime.strptime(request.form.get(k, ""), "%H:%M").time() for k in ("start", "end"))
            wd = int(request.form.get("weekday", ""))
            if e <= s or not 0 <= wd <= 6:
                raise ValueError
            db.session.add(TrainerAvailability(trainer_id=t.id, weekday=wd, start_time=s, end_time=e))
            db.session.commit()
            flash("Availability added.", "ok")
        except ValueError:
            flash("Enter a valid day and a start time before the end time.", "error")
    return redirect(url_for("admin.trainer_edit", tid=t.id))


# ======================================================= website content/media
CONTENT_FIELDS = [
    ("hero_title", "Hero headline (use | for a line break)", "text"), ("hero_subtitle", "Hero subheading", "text"),
    ("hero_video", "Hero video (mp4/webm, ≤100MB)", "video"), ("hero_image_1", "Hero image 1", "image"), ("hero_image_2", "Hero image 2", "image"),
    ("hero_image_3", "Hero image 3", "image"), ("announcement", "Announcement bar (leave empty to hide)", "text"),
    ("experience_title", "Experience headline", "text"), ("experience_text", "Experience text", "textarea"), ("experience_image", "Experience image", "image"),
    ("training_strength_image", "Strength card image", "image"), ("training_performance_image", "Performance card image", "image"),
    ("training_mobility_image", "Mobility card image", "image"), ("cine_video", "Cinematic section video", "video"), ("cine_image", "Cinematic section image", "image"),
    ("cine_title", "Cinematic overlay text", "text"), ("address", "Address", "text"), ("hours", "Opening hours", "text"),
    ("contact_phone", "Phone", "text"), ("contact_email", "Public email", "text"), ("ticker", "Scrolling ticker (separate items with |)", "text"),
]


@bp.route("/content", methods=["GET", "POST"])
def content():
    cur = {c.key: c.value for c in SiteContent.query.all()}
    if request.method == "POST":
        for key, label, typ in CONTENT_FIELDS:
            val = (request.form.get(key) or "").strip()[:2000]
            if typ in ("image", "video"):
                up = request.files.get(key)
                if up and up.filename:
                    val = save_upload(up, "image" if typ == "image" else "any", current_user())
                elif request.form.get(key + "__clear"):
                    val = ""
            row = db.session.get(SiteContent, key)
            if row:
                row.value = val
            else:
                db.session.add(SiteContent(key=key, value=val))
        db.session.commit()
        flash("Website content updated — it's live now.", "ok")
        return redirect(url_for("admin.content"))
    return render_template("admin/content.html", fields=CONTENT_FIELDS, cur=cur, **ctx("content"))


@bp.route("/media", methods=["GET", "POST"])
def media():
    if request.method == "POST":
        n = 0
        for f in request.files.getlist("files"):
            if f and f.filename:
                save_upload(f, "any", current_user())
                n += 1
        db.session.commit()
        flash(f"{n} file(s) uploaded." if n else "Choose at least one file.", "ok" if n else "error")
        return redirect(url_for("admin.media"))
    items, pg = paginate(Media.query.order_by(Media.created_at.desc()), 24)
    return render_template("admin/media.html", items=items, pg=pg, qs=qs, **ctx("media"))


@bp.route("/media/<int:mid>/delete", methods=["POST"])
def media_delete(mid):
    m = db.session.get(Media, mid) or abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], m.filename)
    if os.path.commonpath([os.path.abspath(path), current_app.config["UPLOAD_DIR"]]) == current_app.config["UPLOAD_DIR"] and os.path.exists(path):
        os.remove(path)
    db.session.delete(m)
    db.session.commit()
    flash("File deleted.", "ok")
    return redirect(url_for("admin.media"))
