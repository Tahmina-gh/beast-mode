from datetime import date, datetime, time, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import services as S
from core import ApiError, Form, current_user, login_required
from models import (Attendance, ClassBooking, FitnessGoal, GymClass, Member, Notification, Trainer,
                    TrainerAvailability, TrainerSession, db)

bp = Blueprint("trainer", __name__, url_prefix="/trainer")

NAV = [("home", "Today", "/trainer", "⌂"), ("schedule", "Schedule", "/trainer/schedule", "◷"),
       ("members", "Members", "/trainer/members", "☺"), ("availability", "Availability", "/trainer/availability", "✎"),
       ("checkin", "Check-in", "/staff/checkin", "✓")]
MORE = [("notifications", "Notifications", "/account/notifications"), ("profile", "My profile", "/account/")]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def me():
    u = current_user()
    if not u.trainer:
        abort(403)
    return u.trainer


def ctx(active):
    return dict(nav=NAV, more=MORE, active=active, area="trainer")


@bp.route("/")
@login_required("trainer")
def home():
    t, now = me(), datetime.now()
    d0, d1 = datetime.combine(date.today(), time.min), datetime.combine(date.today() + timedelta(days=1), time.min)
    today_s = TrainerSession.query.filter(TrainerSession.trainer_id == t.id, TrainerSession.starts_at >= d0, TrainerSession.starts_at < d1,
                                          TrainerSession.status.in_(("confirmed", "completed", "no_show", "pending_payment"))).order_by(TrainerSession.starts_at).all()
    today_c = GymClass.query.filter(GymClass.trainer_id == t.id, GymClass.starts_at >= d0, GymClass.starts_at < d1, GymClass.status != "cancelled").order_by(GymClass.starts_at).all()
    nxt = TrainerSession.query.filter(TrainerSession.trainer_id == t.id, TrainerSession.starts_at >= d1, TrainerSession.status == "confirmed").order_by(TrainerSession.starts_at).limit(6).all()
    members = {s.member_id for s in TrainerSession.query.filter_by(trainer_id=t.id).all()} | {m.id for m in Member.query.filter_by(assigned_trainer_id=t.id)}
    notes = Notification.query.filter_by(user_id=t.user_id).order_by(Notification.created_at.desc()).limit(5).all()
    return render_template("trainer/home.html", t=t, today_s=today_s, today_c=today_c, nxt=nxt, n_members=len(members),
                           notes=notes, **ctx("home"))


@bp.route("/schedule")
@login_required("trainer")
def schedule():
    t, now = me(), datetime.now()
    ses = TrainerSession.query.filter(TrainerSession.trainer_id == t.id, TrainerSession.starts_at >= now - timedelta(days=1),
                                      TrainerSession.status.in_(("confirmed", "pending_payment"))).order_by(TrainerSession.starts_at).all()
    classes = GymClass.query.filter(GymClass.trainer_id == t.id, GymClass.starts_at >= now - timedelta(hours=6),
                                    GymClass.status == "scheduled").order_by(GymClass.starts_at).limit(20).all()
    return render_template("trainer/schedule.html", ses=ses, classes=classes, **ctx("schedule"))


@bp.route("/members")
@login_required("trainer")
def members():
    t = me()
    ids = {s.member_id for s in TrainerSession.query.filter_by(trainer_id=t.id).all()} | {m.id for m in Member.query.filter_by(assigned_trainer_id=t.id)}
    ms = Member.query.filter(Member.id.in_(ids)).all() if ids else []
    rows = []
    for m in ms:
        last = Attendance.query.filter_by(member_id=m.id).order_by(Attendance.checked_in_at.desc()).first()
        rows.append({"m": m, "plan": (S.active_membership(m).plan.name if S.active_membership(m) else "No active plan"),
                     "last": last.checked_in_at if last else None, "streak": S.current_streak(m.id)})
    return render_template("trainer/members.html", rows=rows, **ctx("members"))


@bp.route("/members/<int:mid>")
@login_required("trainer", "admin")
def member_progress(mid):
    m = db.session.get(Member, mid) or abort(404)
    u = current_user()
    if not S.can_view_member(u, m):
        abort(403)
    from models import FitnessProgress
    lifts = sorted({e.metric[3:] for e in FitnessProgress.query.filter_by(member_id=m.id).all() if e.metric.startswith("pr:")})
    goals = FitnessGoal.query.filter_by(member_id=m.id).all()
    ctxd = ctx("members") if u.role_name == "trainer" else dict(nav=[], more=[], active="", area="admin")
    return render_template("trainer/member_progress.html", m=m, lifts=lifts, goals=goals, streak=S.current_streak(m.id),
                           weekly=S.weekly_attendance(m.id, 12), **ctxd)


@bp.route("/availability", methods=["GET", "POST"])
@login_required("trainer")
def availability():
    t = me()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            f = Form(request.form)
            f.int_("weekday", "Day", min=0, max=6)
            for k in ("start", "end"):
                try:
                    f.v[k] = datetime.strptime(request.form.get(k, ""), "%H:%M").time()
                except ValueError:
                    f.errors[k] = "Enter a valid time."
            if f.ok and f.v["end"] <= f.v["start"]:
                f.errors["end"] = "End time must be after start time."
            if f.ok:
                db.session.add(TrainerAvailability(trainer_id=t.id, weekday=f.v["weekday"], start_time=f.v["start"], end_time=f.v["end"]))
                db.session.commit()
                flash("Availability added.", "ok")
            else:
                flash(next(iter(f.errors.values())), "error")
        elif action == "delete":
            a = db.session.get(TrainerAvailability, request.form.get("id", type=int) or 0)
            if a and a.trainer_id == t.id:
                db.session.delete(a)
                db.session.commit()
                flash("Removed.", "ok")
        elif action == "settings":
            f = Form(request.form)
            f.int_("session_minutes", "Session length", min=15, max=180)
            if f.ok:
                t.session_minutes = f.v["session_minutes"]
                db.session.commit()
                flash("Saved.", "ok")
            else:
                flash(next(iter(f.errors.values())), "error")
        return redirect(url_for("trainer.availability"))
    rows = sorted(t.availability, key=lambda a: (a.weekday, a.start_time))
    return render_template("trainer/availability.html", t=t, rows=rows, days=DAYS, **ctx("availability"))


# ------------------------------------------------- staff check-in (admin + trainers)
staff = Blueprint("staff", __name__, url_prefix="/staff")


@staff.route("/checkin")
@login_required("admin", "trainer")
def checkin():
    u = current_user()
    from routes_admin import NAV as ANAV, MORE as AMORE
    c = dict(nav=ANAV, more=AMORE, active="attendance", area="admin") if u.role_name == "admin" else ctx("checkin")
    today = (Attendance.query.filter(Attendance.checked_in_at >= datetime.combine(date.today(), time.min))
             .order_by(Attendance.checked_in_at.desc()).limit(25).all())
    return render_template("trainer/checkin.html", today=today, **c)
