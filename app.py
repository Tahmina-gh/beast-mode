import os
import threading
import time as _time
from datetime import date, datetime

import click
from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from core import ApiError, csrf_protect, csrf_token, current_user, media_url, wants_json
from models import Notification, SiteContent, db


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("AUTH_SECRET is not set. Refusing to start in production without a secret key.")
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance"), exist_ok=True)
    db.init_app(app)

    # ------------------------------------------------------------- blueprints
    from routes_admin import bp as admin_bp
    from routes_api import bp as api_bp
    from routes_auth import account as account_bp
    from routes_auth import bp as auth_bp
    from routes_member import bp as member_bp
    from routes_site import bp as site_bp
    from routes_trainer import bp as trainer_bp
    from routes_trainer import staff as staff_bp
    for b in (site_bp, auth_bp, account_bp, member_bp, trainer_bp, staff_bp, admin_bp, api_bp):
        app.register_blueprint(b)

    _job_state = {"last": 0.0, "running": False}

    @app.before_request
    def _opportunistic_jobs():
        """For hosts with no cron (e.g. PythonAnywhere free): run the idempotent reminder/expiry jobs
        in the background at most once an hour, triggered by a visitor. Disable: RUN_JOBS_ON_REQUEST=0"""
        if app.config.get("TESTING") or os.environ.get("RUN_JOBS_ON_REQUEST", "1") != "1":
            return
        if _job_state["running"] or _time.time() - _job_state["last"] < 3600:
            return
        _job_state["running"], _job_state["last"] = True, _time.time()

        def _go():
            try:
                with app.app_context():
                    from services import run_jobs
                    run_jobs()
            except Exception as e:  # never affect visitors
                print("background jobs error:", e)
            finally:
                _job_state["running"] = False
        threading.Thread(target=_go, daemon=True).start()

    @app.before_request
    def _reset_user_cache():
        g.pop("_cu", None)  # never reuse a cached user across requests (must return None)

    app.before_request(csrf_protect)

    # -------------------------------------------------------- template helpers
    app.jinja_env.globals.update(csrf_token=csrf_token, cu=current_user, media_url=media_url, now=datetime.now, enumerate=enumerate)
    app.jinja_env.filters["money"] = lambda c, cur=None: f"{(c or 0) / 100:,.2f}".rstrip("0").rstrip(".") if (c or 0) % 100 == 0 else f"{(c or 0) / 100:,.2f}"
    app.jinja_env.filters["fdt"] = lambda d: d.strftime("%a %d %b · %I:%M %p").replace(" 0", " ") if d else "—"
    app.jinja_env.filters["fdate"] = lambda d: d.strftime("%d %b %Y") if d else "—"
    app.jinja_env.filters["ftime"] = lambda d: d.strftime("%I:%M %p").lstrip("0") if d else "—"
    app.jinja_env.filters["initials"] = lambda n: "".join(p[0] for p in (n or "?").split()[:2]).upper()
    app.jinja_env.filters["getattr"] = lambda obj, name: getattr(obj, name, None)

    @app.context_processor
    def inject():
        u = current_user()
        unread = Notification.query.filter_by(user_id=u.id, read_at=None).count() if u else 0
        content = {}
        if request.endpoint and (request.endpoint.startswith("site.") or request.endpoint.startswith("auth.")):
            content = {c.key: c.value for c in SiteContent.query.all()}
        return dict(gym_name=app.config["GYM_NAME"], unread=unread, content=content,
                    stripe_on=bool(app.config["STRIPE_SECRET_KEY"]), currency=app.config["CURRENCY"].upper())

    # -------------------------------------------------------- security headers
    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data: blob: https://images.unsplash.com; "
                                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                "script-src 'self' 'unsafe-inline'; media-src 'self' blob:; connect-src 'self'; "
                                "form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'; base-uri 'self'")
        if app.config["IS_PROD"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith(("/member", "/trainer", "/admin", "/api", "/account")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------------------------------------------------------- uploads
    @app.route("/uploads/<path:name>")
    def uploads(name):
        return send_from_directory(app.config["UPLOAD_DIR"], name, max_age=3600)

    # ----------------------------------------------------------------- errors
    @app.errorhandler(ApiError)
    def api_error(e):
        if wants_json():
            return jsonify(ok=False, error=e.message, code=e.code, fields=e.fields), e.status
        flash(e.message, "error")
        return redirect(request.referrer or url_for("site.index"))

    def _err(code, title, msg):
        if wants_json():
            return jsonify(ok=False, error=msg), code
        return render_template("error.html", code=code, title=title, msg=msg), code

    app.register_error_handler(404, lambda e: _err(404, "Page not found", "That page doesn't exist."))
    app.register_error_handler(403, lambda e: _err(403, "Not allowed", "You don't have access to this page."))
    app.register_error_handler(413, lambda e: _err(413, "File too large", "That upload is too large."))
    app.register_error_handler(405, lambda e: _err(405, "Not allowed", "That action isn't supported."))

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return _err(500, "Something went wrong", "We've logged the problem. Please try again in a moment.")

    with app.app_context():
        db.create_all()
        from seed import ensure_roles
        ensure_roles()

    register_cli(app)
    return app


def register_cli(app):
    from seed import (create_admin, purge_demo, seed_baseline, seed_demo)

    @app.cli.command("create-admin")
    @click.option("--email", default=lambda: os.environ.get("ADMIN_EMAIL", ""))
    @click.option("--password", default=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    @click.option("--name", default="Gym Admin")
    def _create_admin(email, password, name):
        """Create the first real admin account."""
        click.echo(create_admin(email, password, name))

    @app.cli.command("seed-baseline")
    def _baseline():
        """Production-safe starter data: default site copy, plans, FAQs, quiz rules. No fake people."""
        click.echo(seed_baseline())

    @app.cli.command("seed-demo")
    @click.option("--force", is_flag=True, help="Allow in production (not recommended)")
    def _demo(force):
        """DEVELOPMENT ONLY demo data: fake members/trainers/bookings. All demo users end in @demo.beastmode.test"""
        if app.config["IS_PROD"] and not force:
            raise click.ClickException("Refusing to seed demo data in production. Use --force if you really mean it.")
        click.echo(seed_demo())

    @app.cli.command("purge-demo")
    def _purge():
        """Delete every demo user and their data."""
        click.echo(purge_demo())

    @app.cli.command("jobs")
    def _jobs():
        """Expiry checks, reminders, waitlist expiry. Schedule hourly (cron)."""
        from services import run_jobs
        click.echo(run_jobs())


app = create_app() if os.environ.get("FLASK_APP_AUTOCREATE", "1") == "1" else None

if __name__ == "__main__":
    import threading
    import time as _t

    def _scheduler():
        from services import run_jobs
        while True:
            _t.sleep(3600)
            with app.app_context():
                try:
                    run_jobs()
                except Exception as e:
                    print("job error:", e)

    if os.environ.get("RUN_SCHEDULER", "1") == "1":
        threading.Thread(target=_scheduler, daemon=True).start()
    app.run(debug=not Config.IS_PROD, port=int(os.environ.get("PORT", 5000)))
