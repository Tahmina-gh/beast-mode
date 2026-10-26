from datetime import datetime, timedelta

from flask import Blueprint, render_template, request

from models import FAQ, Facility, GymClass, MembershipPlan, Testimonial, Trainer, db
from services import track

bp = Blueprint("site", __name__)


@bp.route("/")
def index():
    if request.headers.get("DNT") != "1" and "bot" not in (request.user_agent.string or "").lower():
        track("page_view", "/")
    classes = (GymClass.query.filter(GymClass.status == "scheduled", GymClass.starts_at > datetime.now(),
                                     GymClass.starts_at < datetime.now() + timedelta(days=14))
               .order_by(GymClass.starts_at).limit(12).all())
    return render_template(
        "site/index.html",
        classes=classes,
        categories=sorted({c.category for c in classes}),
        trainers=Trainer.query.filter_by(active=True).order_by(Trainer.sort, Trainer.id).all(),
        plans=MembershipPlan.query.filter_by(status="active").order_by(MembershipPlan.sort, MembershipPlan.price_monthly_cents).all(),
        testimonials=Testimonial.query.filter_by(published=True).order_by(Testimonial.created_at.desc()).all(),
        facilities=Facility.query.filter_by(published=True).order_by(Facility.sort, Facility.id).all(),
        faqs=FAQ.query.filter_by(published=True).order_by(FAQ.sort, FAQ.id).all(),
    )


@bp.route("/terms")
def terms():
    return render_template("site/terms.html")
