"""Seed data. `seed_baseline` is production-safe (real starter content, no fake people).
`seed_demo` creates realistic fake data for development only — every demo user's email
ends in @demo.beastmode.test and every demo row is flagged so `purge_demo` can remove it cleanly.
"""
import random
import secrets
from datetime import date, datetime, time, timedelta

from werkzeug.security import generate_password_hash

from models import (FAQ, Attendance, ClassBooking, Facility, GymClass, Member, Membership, MembershipPlan, Payment,
                    QuizRule, Role, SiteContent, Testimonial, Trainer, TrainerAvailability, TrainerSession,
                    TourBooking, TrialLead, User, db)

DEMO_DOMAIN = "demo.beastmode.test"
IMG = "https://images.unsplash.com/photo-{}?fm=jpg&q=75&w={}&auto=format&fit=crop"


def ensure_roles():
    for name in ("member", "trainer", "admin"):
        if not Role.query.filter_by(name=name).first():
            db.session.add(Role(name=name))
    db.session.commit()


def create_admin(email, password, name="Gym Admin"):
    if not email or not password:
        return "Usage: flask --app app create-admin --email you@gym.com --password 'Something123'"
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if User.query.filter_by(email=email).first():
        return f"An account with {email} already exists."
    role = Role.query.filter_by(name="admin").first()
    db.session.add(User(email=email, password_hash=generate_password_hash(password), role_id=role.id,
                        full_name=name, email_verified=True))
    db.session.commit()
    return f"Admin account created: {email}"


def _content(defaults):
    for k, v in defaults.items():
        if not db.session.get(SiteContent, k):
            db.session.add(SiteContent(key=k, value=v))


def seed_baseline():
    if MembershipPlan.query.count() > 0:
        return "Baseline already present — skipped (safe to re-run any time)."
    db.session.add_all([
        MembershipPlan(name="Essential", description="Everything you need to start training properly.",
                       price_monthly_cents=3900, price_yearly_cents=39000, duration_days=30, gym_access="Off-peak",
                       classes_included=2, pt_sessions=0, guest_passes=0, recovery_access=False,
                       benefits="Gym floor access (off-peak)\n2 classes per month\nFree app & progress tracking", sort=1),
        MembershipPlan(name="Performance", description="Our most popular plan — unlimited training, real support.",
                       price_monthly_cents=6900, price_yearly_cents=69000, duration_days=30, gym_access="24/7",
                       classes_included=-1, pt_sessions=2, guest_passes=2, recovery_access=True, featured=True,
                       benefits="24/7 gym access\nUnlimited classes\n2 personal training sessions / month\nRecovery zone access\n2 guest passes / month", sort=2),
        MembershipPlan(name="Elite", description="Full access, weekly coaching, priority on everything.",
                       price_monthly_cents=11900, price_yearly_cents=119000, duration_days=30, gym_access="24/7",
                       classes_included=-1, pt_sessions=4, guest_passes=-1, recovery_access=True,
                       benefits="24/7 gym access\nUnlimited classes\nWeekly personal training\nPriority recovery zone access\nUnlimited guest passes", sort=3),
    ])
    db.session.add_all([
        FAQ(question="Do I need to book classes in advance?", answer="Yes — book through your member dashboard or the website. Spots are limited and confirmed first-come, first-served.", sort=1),
        FAQ(question="Can I freeze or cancel my membership?", answer="You can cancel any time from your dashboard; you'll keep access until the end of your paid period. There's no freeze option yet — contact the front desk for exceptions.", sort=2),
        FAQ(question="What happens if a class is full?", answer="You can join the waitlist. If a spot opens up you'll get a notification and a short window to claim it before it goes to the next person.", sort=3),
        FAQ(question="Do personal training sessions carry over if I don't use them?", answer="Included sessions reset each billing month and don't roll over. Extra sessions can be purchased any time.", sort=4),
    ])
    db.session.add_all([
        Facility(name="Weight Floor", description="Full dumbbell range, power racks and Olympic platforms across an open, high-ceiling floor.",
                 image=IMG.format("1637430308606-86576d8fef3c", 900), pos_x=22, pos_y=60, sort=1),
        Facility(name="Cardio", description="Rowers, bikes and treadmills, positioned along the window wall for natural light.",
                 image=IMG.format("1639660299469-9e9279364c97", 900), pos_x=45, pos_y=35, sort=2),
        Facility(name="Functional", description="Turf lane, sleds, kettlebells and battle ropes for circuit and athletic conditioning.",
                 image=IMG.format("1589579234096-25cb6b83e021", 900), pos_x=66, pos_y=55, sort=3),
        Facility(name="Boxing", description="Heavy bags and a sprung floor for pad work, technique classes and conditioning.",
                 image=IMG.format("1708793637823-1f9d7f7c7749", 900), pos_x=80, pos_y=30, sort=4),
        Facility(name="Recovery", description="Sauna, cold plunge and stretch benches to close out every session.",
                 image=IMG.format("1637430308606-86576d8fef3c", 900), pos_x=88, pos_y=75, sort=5),
    ])
    db.session.add_all([
        QuizRule(goal="Build muscle", experience="any", min_days=1, max_days=7, class_category="Strength", trainer_specialization="Strength",
                priority=2, program="A 4-day upper/lower strength split, progressive overload each week, with 2 accessory days for weak points."),
        QuizRule(goal="Lose weight", experience="any", min_days=1, max_days=7, class_category="HIIT", trainer_specialization="Performance",
                priority=2, program="3-4 HIIT/conditioning sessions a week paired with 2 strength sessions to protect muscle while you cut."),
        QuizRule(goal="Improve strength", experience="any", min_days=1, max_days=7, class_category="Strength", trainer_specialization="Strength",
                priority=2, program="A linear strength program built around the big compound lifts, 3-4 days a week."),
        QuizRule(goal="Improve endurance", experience="any", min_days=1, max_days=7, class_category="Cardio", trainer_specialization="Performance",
                priority=2, program="Zone 2 cardio base-building plus one interval session a week, building volume gradually."),
        QuizRule(goal="Improve mobility", experience="any", min_days=1, max_days=7, class_category="Mobility", trainer_specialization="Mobility",
                priority=2, program="Daily 15-minute mobility flows plus 2 dedicated mobility classes a week."),
        QuizRule(goal="General fitness", experience="any", min_days=1, max_days=7, class_category="Functional Training", trainer_specialization="",
                priority=1, program="A balanced mix of strength, conditioning and mobility — 3 sessions a week to start."),
    ])
    _content({
        "hero_title": "BUILT FOR|MORE.", "hero_subtitle": "Train stronger. Move better. Become your strongest self.",
        "announcement": "", "experience_title": "Not just a gym.",
        "experience_text": "An environment designed to make every workout count — considered lighting, real equipment, and space that gets out of your way.",
        "cine_title": "Every rep has a purpose.", "address": "DHA Phase 6, Lahore", "hours": "Open 24/7",
        "contact_phone": "+92 300 0000000", "contact_email": "hello@beastmode.gym",
        "ticker": "STRENGTH TRAINING|BOXING|FREE WEIGHTS|CARDIO|MOBILITY|24/7 ACCESS",
    })
    db.session.commit()
    return "Baseline seeded: 3 membership plans, FAQs, facilities and goal-quiz rules. No fake people were created."


def _demo_user(role_name, full_name, email_local, **kw):
    role = Role.query.filter_by(name=role_name).first()
    u = User(email=f"{email_local}@{DEMO_DOMAIN}", password_hash=generate_password_hash("Demo12345"), role_id=role.id,
             full_name=full_name, email_verified=True, is_demo=True, **kw)
    db.session.add(u)
    db.session.flush()
    return u


def seed_demo():
    if User.query.filter(User.email.like(f"%@{DEMO_DOMAIN}")).first():
        return "Demo data already present — run `flask --app app purge-demo` first if you want to reseed."
    if MembershipPlan.query.count() == 0:
        seed_baseline()
    rnd = random.Random(42)
    plans = MembershipPlan.query.order_by(MembershipPlan.id).all()

    trainers = []
    specs = [("Marcus Reed", "Strength & Conditioning", 9, "Former competitive powerlifter focused on clean technique over shortcuts.", "1597347324655-f99149f2e285"),
             ("Elena Cruz", "Performance & Mobility", 6, "Works with athletes recovering from injury and anyone who wants to move without pain.", "1639660299469-9e9279364c97"),
             ("Jay Okafor", "Boxing & Conditioning", 7, "Ex-amateur boxer; pad work, footwork and engine-building conditioning.", "1708793637823-1f9d7f7c7749"),
             ("Sara Malik", "Strength", 5, "Coaches the big compound lifts with a focus on long-term joint health.", "1513352098199-8ccf457b35a8"),
             ("Dee Park", "Mobility & Recovery", 4, "Physio background; mobility flows and return-to-training programming.", "1589579234096-25cb6b83e021")]
    for i, (name, spec, yrs, bio, img) in enumerate(specs):
        u = _demo_user("trainer", name, f"trainer{i+1}")
        t = Trainer(user_id=u.id, specialization=spec, bio=bio, experience_years=yrs, photo=IMG.format(img, 600),
                    session_minutes=60, session_price_cents=6000 + i * 500, sort=i)
        db.session.add(t)
        db.session.flush()
        for wd in (0, 1, 2, 3, 4):
            db.session.add(TrainerAvailability(trainer_id=t.id, weekday=wd, start_time=time(9, 0), end_time=time(17, 0)))
        trainers.append(t)
    db.session.commit()

    classes = []
    cats = ["Strength", "HIIT", "Yoga", "Boxing", "CrossFit", "Cardio", "Mobility", "Functional Training"]
    start = datetime.combine(date.today(), time(6, 0))
    for d in range(14):
        for slot_h in (6, 9, 12, 17, 18, 19):
            if rnd.random() < 0.55:
                cat = rnd.choice(cats)
                trainer = rnd.choice(trainers)
                st = start + timedelta(days=d, hours=slot_h - 6)
                cap = rnd.choice([10, 12, 15, 20])
                c = GymClass(name=f"{cat} {'AM' if slot_h < 12 else 'PM'}", category=cat, trainer_id=trainer.id,
                            starts_at=st, ends_at=st + timedelta(minutes=45), capacity=cap, difficulty=rnd.randint(1, 5),
                            location=rnd.choice(["Main Floor", "Studio A", "Boxing Area", "Functional Area"]))
                db.session.add(c)
                classes.append(c)
    db.session.commit()

    members = []
    first = ["Ali", "Sana", "Hamza", "Ayesha", "Bilal", "Zara", "Omar", "Fatima", "Usman", "Mahnoor", "Ahmed", "Hira"]
    for i in range(12):
        u = _demo_user("member", f"{first[i]} Demo", f"member{i+1}", phone=f"+9230000{1000+i}")
        m = Member(user_id=u.id, member_code=f"BM-{900000+i}", qr_token=secrets.token_urlsafe(24),
                  assigned_trainer_id=trainers[i % len(trainers)].id, joined_at=datetime.now() - timedelta(days=rnd.randint(10, 400)))
        db.session.add(m)
        db.session.flush()
        plan = rnd.choice(plans)
        start_d = date.today() - timedelta(days=rnd.randint(0, 20))
        mem = Membership(member_id=m.id, plan_id=plan.id, billing="monthly", kind="new", status="active",
                         payment_status="paid", start_date=start_d, end_date=start_d + timedelta(days=30), renewal_date=start_d + timedelta(days=30))
        db.session.add(mem)
        db.session.flush()
        db.session.add(Payment(user_id=u.id, purpose="membership", membership_id=mem.id, amount_cents=plan.price_monthly_cents,
                               currency="usd", provider="manual", transaction_id=f"DEMO-{1000+i}", status="paid",
                               receipt_no=f"BM-DEMO-{1000+i}", description=f"{plan.name} membership (monthly)", paid_at=datetime.now() - timedelta(days=rnd.randint(0, 20))))
        for _ in range(rnd.randint(4, 25)):
            day = date.today() - timedelta(days=rnd.randint(0, 45))
            tin = datetime.combine(day, time(rnd.randint(6, 20), rnd.choice([0, 15, 30, 45])))
            db.session.add(Attendance(member_id=m.id, checked_in_at=tin, checked_out_at=tin + timedelta(minutes=rnd.randint(40, 100)), method="qr"))
        members.append(m)
    db.session.commit()

    for c in classes:
        if c.starts_at > datetime.now() - timedelta(days=2):
            for m in rnd.sample(members, k=min(rnd.randint(0, c.capacity), len(members))):
                if c.booked >= c.capacity:
                    break
                db.session.add(ClassBooking(class_id=c.id, member_id=m.id, status="booked" if c.starts_at > datetime.now() else rnd.choice(["attended", "no_show"])))
                c.booked += 1
    db.session.commit()

    for i in range(10):
        m, t = rnd.choice(members), rnd.choice(trainers)
        st = datetime.now() + timedelta(days=rnd.randint(-5, 10), hours=rnd.randint(9, 16))
        db.session.add(TrainerSession(member_id=m.id, trainer_id=t.id, service="Personal Training", starts_at=st,
                                      ends_at=st + timedelta(minutes=60), price_cents=0, included=True,
                                      status="completed" if st < datetime.now() else "confirmed",
                                      slot_lock=f"demo-{i}-{secrets.token_hex(4)}"))
    db.session.add_all([
        TrialLead(name="Noah Fisher", email=f"noah@{DEMO_DOMAIN}", phone="+923001112233", age=27, goal="Build muscle",
                  preferred_date=date.today() + timedelta(days=2), preferred_time="Evening", experience="beginner", status="NEW"),
        TrialLead(name="Layla Ahmed", email=f"layla@{DEMO_DOMAIN}", phone="+923004445566", age=31, goal="Lose weight",
                  preferred_date=date.today() + timedelta(days=1), preferred_time="Morning", experience="intermediate", status="CONTACTED"),
        TourBooking(name="Chris Dean", email=f"chris@{DEMO_DOMAIN}", phone="+923007778899", tour_date=date.today() + timedelta(days=3), tour_time="17:00", status="pending"),
    ])
    db.session.add_all([
        Testimonial(name="Ali Demo", rating=5, text="Down 8kg and stronger than I've ever been. The coaches actually watch your form.",
                   goal="Lose weight", duration="16 weeks", result="-8kg, first pull-up", published=True, photo=IMG.format("1513352098199-8ccf457b35a8", 300)),
        Testimonial(name="Sana Demo", rating=5, text="Booking classes takes ten seconds and I've never once shown up to a full class I wasn't expecting.",
                   goal="General fitness", duration="6 months", result="Consistent 4x/week", published=True, photo=IMG.format("1639660299469-9e9279364c97", 300)),
    ])
    db.session.commit()
    return f"Demo data seeded: {len(trainers)} trainers, {len(members)} members, {len(classes)} classes, plus bookings, attendance and leads."


def purge_demo():
    users = User.query.filter(User.email.like(f"%@{DEMO_DOMAIN}")).all()
    n = len(users)
    for u in users:
        db.session.delete(u)  # cascades to member/trainer rows and their children
    for l in TrialLead.query.filter(TrialLead.email.like(f"%@{DEMO_DOMAIN}")):
        db.session.delete(l)
    for t in TourBooking.query.filter(TourBooking.email.like(f"%@{DEMO_DOMAIN}")):
        db.session.delete(t)
    for t in Testimonial.query.filter(Testimonial.name.ilike("%Demo")):
        db.session.delete(t)
    db.session.commit()
    return f"Removed {n} demo user(s) and everything linked to them."
