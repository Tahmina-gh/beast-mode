"""Relational schema. Money is stored as integer cents. Datetimes are naive gym-local time."""
import sqlite3
from datetime import datetime, date

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


def now():
    return datetime.now()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()


# ------------------------------------------------------------------ identity
class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), unique=True, nullable=False)  # member | trainer | admin


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(190), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30))
    photo = db.Column(db.String(255))
    status = db.Column(db.String(12), nullable=False, default="active")  # active | suspended
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    is_demo = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=now)
    last_login = db.Column(db.DateTime)
    role = db.relationship("Role")

    @property
    def role_name(self):
        return self.role.name

    @property
    def first_name(self):
        return (self.full_name or "").split(" ")[0]


class Member(db.Model):
    __tablename__ = "members"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    member_code = db.Column(db.String(20), unique=True, nullable=False)
    qr_token = db.Column(db.String(64), unique=True, nullable=False)
    dob = db.Column(db.Date)
    emergency_contact = db.Column(db.String(160))
    assigned_trainer_id = db.Column(db.Integer, db.ForeignKey("trainers.id", ondelete="SET NULL"))
    joined_at = db.Column(db.DateTime, default=now)
    user = db.relationship("User", backref=db.backref("member", uselist=False, passive_deletes=True))
    assigned_trainer = db.relationship("Trainer", foreign_keys=[assigned_trainer_id])


class Trainer(db.Model):
    __tablename__ = "trainers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    specialization = db.Column(db.String(160), nullable=False)
    bio = db.Column(db.Text, default="")
    experience_years = db.Column(db.Integer, default=0)
    certifications = db.Column(db.Text, default="")
    photo = db.Column(db.String(255))
    session_minutes = db.Column(db.Integer, nullable=False, default=60)
    session_price_cents = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    sort = db.Column(db.Integer, default=0)
    user = db.relationship("User", backref=db.backref("trainer", uselist=False, passive_deletes=True))

    @property
    def name(self):
        return self.user.full_name


class TrainerAvailability(db.Model):
    __tablename__ = "trainer_availability"
    id = db.Column(db.Integer, primary_key=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey("trainers.id", ondelete="CASCADE"), nullable=False, index=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Mon
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    trainer = db.relationship("Trainer", backref=db.backref("availability", cascade="all, delete-orphan"))


# ---------------------------------------------------------------- memberships
class MembershipPlan(db.Model):
    __tablename__ = "membership_plans"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, default="")
    price_monthly_cents = db.Column(db.Integer, nullable=False, default=0)
    price_yearly_cents = db.Column(db.Integer, nullable=False, default=0)
    duration_days = db.Column(db.Integer, nullable=False, default=30)
    gym_access = db.Column(db.String(60), default="24/7")
    classes_included = db.Column(db.Integer, nullable=False, default=-1)  # per month, -1 = unlimited
    pt_sessions = db.Column(db.Integer, nullable=False, default=0)  # per month
    guest_passes = db.Column(db.Integer, nullable=False, default=0)  # per month
    recovery_access = db.Column(db.Boolean, nullable=False, default=False)
    benefits = db.Column(db.Text, default="")  # one per line
    status = db.Column(db.String(12), nullable=False, default="active")  # active | draft | archived
    featured = db.Column(db.Boolean, default=False)
    sort = db.Column(db.Integer, default=0)

    @property
    def benefit_list(self):
        return [b.strip() for b in (self.benefits or "").splitlines() if b.strip()]


class Membership(db.Model):
    __tablename__ = "memberships"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("membership_plans.id"), nullable=False)
    billing = db.Column(db.String(10), nullable=False, default="monthly")
    kind = db.Column(db.String(12), nullable=False, default="new")  # new|renew|upgrade|downgrade
    status = db.Column(db.String(12), nullable=False, default="pending")  # pending|active|cancelled|expired|replaced
    payment_status = db.Column(db.String(12), nullable=False, default="pending")
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date, index=True)
    renewal_date = db.Column(db.Date)
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)
    reminders_sent = db.Column(db.String(30), default="")
    created_at = db.Column(db.DateTime, default=now)
    member = db.relationship("Member", backref="memberships")
    plan = db.relationship("MembershipPlan")


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose = db.Column(db.String(12), nullable=False)  # membership | session
    membership_id = db.Column(db.Integer, db.ForeignKey("memberships.id", ondelete="SET NULL"))
    session_id = db.Column(db.Integer, db.ForeignKey("trainer_sessions.id", ondelete="SET NULL"))
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="usd")
    provider = db.Column(db.String(12), nullable=False, default="manual")  # stripe | manual
    provider_ref = db.Column(db.String(120), index=True)  # stripe checkout session id
    transaction_id = db.Column(db.String(120))  # stripe payment_intent / manual reference
    status = db.Column(db.String(12), nullable=False, default="pending")  # pending|paid|failed|refunded
    refund_status = db.Column(db.String(12), nullable=False, default="none")  # none|requested|refunded
    receipt_no = db.Column(db.String(30), unique=True)
    description = db.Column(db.String(200))
    failure_reason = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=now, index=True)
    paid_at = db.Column(db.DateTime)
    user = db.relationship("User")
    membership = db.relationship("Membership")
    session = db.relationship("TrainerSession")


# ----------------------------------------------------------- classes & sessions
class GymClass(db.Model):
    __tablename__ = "gym_classes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(40), nullable=False, default="Strength")
    description = db.Column(db.Text, default="")
    trainer_id = db.Column(db.Integer, db.ForeignKey("trainers.id", ondelete="SET NULL"), index=True)
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    ends_at = db.Column(db.DateTime, nullable=False)
    capacity = db.Column(db.Integer, nullable=False, default=20)
    booked = db.Column(db.Integer, nullable=False, default=0)  # confirmed + held offers
    difficulty = db.Column(db.Integer, nullable=False, default=3)
    location = db.Column(db.String(80), default="Main Floor")
    image = db.Column(db.String(255))
    status = db.Column(db.String(12), nullable=False, default="scheduled")  # scheduled|cancelled|completed
    trainer = db.relationship("Trainer")
    __table_args__ = (
        db.CheckConstraint("booked >= 0 AND booked <= capacity", name="ck_class_capacity"),
    )

    @property
    def remaining(self):
        return max(self.capacity - self.booked, 0)

    @property
    def is_full(self):
        return self.booked >= self.capacity

    @property
    def duration_minutes(self):
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


class ClassBooking(db.Model):
    __tablename__ = "class_bookings"
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey("gym_classes.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    # booked | waitlist | offered | cancelled | expired | attended | no_show
    status = db.Column(db.String(12), nullable=False, default="booked")
    created_at = db.Column(db.DateTime, default=now)
    cancelled_at = db.Column(db.DateTime)
    offer_expires_at = db.Column(db.DateTime)
    reminded = db.Column(db.Boolean, nullable=False, default=False)
    gym_class = db.relationship("GymClass", backref="bookings")
    member = db.relationship("Member")
    __table_args__ = (UniqueConstraint("class_id", "member_id", name="uq_booking_class_member"),)


class TrainerSession(db.Model):
    __tablename__ = "trainer_sessions"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    trainer_id = db.Column(db.Integer, db.ForeignKey("trainers.id", ondelete="CASCADE"), nullable=False, index=True)
    service = db.Column(db.String(60), nullable=False, default="Personal Training")
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    ends_at = db.Column(db.DateTime, nullable=False)
    price_cents = db.Column(db.Integer, nullable=False, default=0)
    included = db.Column(db.Boolean, nullable=False, default=False)  # covered by plan allowance
    # pending_payment | confirmed | completed | cancelled | no_show
    status = db.Column(db.String(16), nullable=False, default="confirmed")
    slot_lock = db.Column(db.String(60), unique=True)  # "trainer|start" while active -> prevents double booking
    notes = db.Column(db.Text)
    reminded = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=now)
    member = db.relationship("Member")
    trainer = db.relationship("Trainer")


class Attendance(db.Model):
    __tablename__ = "attendance"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="CASCADE"), nullable=False)
    checked_in_at = db.Column(db.DateTime, nullable=False, default=now)
    checked_out_at = db.Column(db.DateTime)
    method = db.Column(db.String(12), default="qr")  # qr | member_id | manual
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    member = db.relationship("Member")
    __table_args__ = (Index("ix_attendance_member_time", "member_id", "checked_in_at"),)


# --------------------------------------------------------------------- leads
class TrialLead(db.Model):
    __tablename__ = "trial_leads"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=False)
    age = db.Column(db.Integer)
    goal = db.Column(db.String(60))
    preferred_date = db.Column(db.Date)
    preferred_time = db.Column(db.String(20))
    experience = db.Column(db.String(20))
    # NEW|CONTACTED|TRIAL BOOKED|TRIAL COMPLETED|CONVERTED|NOT INTERESTED
    status = db.Column(db.String(20), nullable=False, default="NEW")
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now, index=True)


class TourBooking(db.Model):
    __tablename__ = "tour_bookings"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    tour_date = db.Column(db.Date, nullable=False)
    tour_time = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="pending")  # pending|approved|cancelled|completed
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now, index=True)


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), nullable=False)
    phone = db.Column(db.String(30))
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(12), nullable=False, default="NEW")  # NEW|READ|RESPONDED|CLOSED
    created_at = db.Column(db.DateTime, default=now, index=True)


# ----------------------------------------------------------------- progress
class FitnessGoal(db.Model):
    __tablename__ = "fitness_goals"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    goal = db.Column(db.String(200), nullable=False)
    target_date = db.Column(db.Date)
    status = db.Column(db.String(12), nullable=False, default="active")  # active|achieved|dropped
    created_at = db.Column(db.DateTime, default=now)


class FitnessProgress(db.Model):
    __tablename__ = "fitness_progress"
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="CASCADE"), nullable=False)
    metric = db.Column(db.String(40), nullable=False)  # weight|body_fat|chest|waist|hips|arm|thigh|pr:<lift>
    value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(8), default="")
    recorded_on = db.Column(db.Date, nullable=False, default=date.today)
    note = db.Column(db.String(200))
    __table_args__ = (Index("ix_progress_member_metric_date", "member_id", "metric", "recorded_on"),)


class QuizResponse(db.Model):
    __tablename__ = "quiz_responses"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    goal = db.Column(db.String(40), nullable=False)
    experience = db.Column(db.String(20), nullable=False)
    days_per_week = db.Column(db.Integer, nullable=False)
    preferred_training = db.Column(db.String(40))
    available_time = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=now)


class QuizRule(db.Model):
    """Admin-configurable recommendation rules."""
    __tablename__ = "quiz_rules"
    id = db.Column(db.Integer, primary_key=True)
    goal = db.Column(db.String(40), nullable=False, default="any")
    experience = db.Column(db.String(20), nullable=False, default="any")
    min_days = db.Column(db.Integer, nullable=False, default=1)
    max_days = db.Column(db.Integer, nullable=False, default=7)
    plan_id = db.Column(db.Integer, db.ForeignKey("membership_plans.id", ondelete="SET NULL"))
    class_category = db.Column(db.String(40))
    trainer_specialization = db.Column(db.String(60))
    program = db.Column(db.Text, default="")
    priority = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    plan = db.relationship("MembershipPlan")


# --------------------------------------------------------------- comms/content
class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text)
    link = db.Column(db.String(200))
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now)
    __table_args__ = (Index("ix_notif_user_read", "user_id", "read_at"),)


class EmailLog(db.Model):
    __tablename__ = "email_log"
    id = db.Column(db.Integer, primary_key=True)
    to_email = db.Column(db.String(190), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="queued")  # sent|failed|not_configured
    error = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=now)


class Testimonial(db.Model):
    __tablename__ = "testimonials"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    photo = db.Column(db.String(255))
    rating = db.Column(db.Integer, nullable=False, default=5)
    text = db.Column(db.Text, nullable=False)
    goal = db.Column(db.String(100))
    duration = db.Column(db.String(60))
    result = db.Column(db.String(120))
    published = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=now)


class Facility(db.Model):
    __tablename__ = "facilities"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, default="")
    image = db.Column(db.String(255))
    pos_x = db.Column(db.Integer)  # % position for the interactive floor tour
    pos_y = db.Column(db.Integer)
    sort = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, nullable=False, default=True)


class FAQ(db.Model):
    __tablename__ = "faqs"
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(240), nullable=False)
    answer = db.Column(db.Text, nullable=False)
    sort = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, nullable=False, default=True)


class Media(db.Model):
    __tablename__ = "media"
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), unique=True, nullable=False)
    original_name = db.Column(db.String(200))
    kind = db.Column(db.String(10), nullable=False)  # image | video
    size = db.Column(db.Integer)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=now)


class SiteContent(db.Model):
    __tablename__ = "site_content"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text, default="")


class AnalyticsEvent(db.Model):
    """Privacy-friendly: no IPs, no user agents, no user ids."""
    __tablename__ = "analytics_events"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), nullable=False)
    path = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=now, index=True)
    __table_args__ = (Index("ix_analytics_name_time", "name", "created_at"),)
