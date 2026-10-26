import smtplib
import threading
from email.message import EmailMessage
from html import escape

from flask import current_app

from models import EmailLog, Notification, Role, User, db


def email_html(title, lines, cta_text=None, cta_url=None):
    """Branded transactional email. All dynamic text is escaped."""
    name = escape(current_app.config["GYM_NAME"])
    body = "".join(f'<p style="margin:0 0 14px;color:#cfcfc9;font-size:15px;line-height:1.6">{escape(l)}</p>' for l in lines)
    btn = (f'<p style="margin:22px 0 4px"><a href="{escape(cta_url)}" style="background:#ff4d1a;color:#fff;'
           f'padding:13px 26px;text-decoration:none;font-weight:700;font-size:13px;letter-spacing:.08em">'
           f'{escape(cta_text.upper())}</a></p>') if cta_text and cta_url else ""
    return (f'<div style="background:#0a0a0b;padding:28px 12px;font-family:Arial,sans-serif"><div style="max-width:520px;'
            f'margin:auto;background:#161618;padding:30px;border-top:3px solid #ff6a1a"><div style="color:#ff6a1a;font-weight:800;'
            f'letter-spacing:.14em;font-size:12px;margin-bottom:18px">{name}</div><h2 style="color:#fff;margin:0 0 16px;'
            f'font-size:22px">{escape(title)}</h2>{body}{btn}</div></div>')


def _deliver(app, log_id, to, subject, html):
    with app.app_context():
        cfg = app.config
        log = db.session.get(EmailLog, log_id)
        if not cfg["SMTP_HOST"]:
            log.status, log.error = "not_configured", "SMTP not configured - email stored only"
            print(f"\n[email:not configured] to={to} subject={subject}\n")
        else:
            try:
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = cfg["EMAIL_FROM"], to, subject
                msg.set_content("Please view this email in an HTML-capable client.")
                msg.add_alternative(html, subtype="html")
                with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as s:
                    s.starttls()
                    if cfg["SMTP_USER"]:
                        s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
                    s.send_message(msg)
                log.status = "sent"
            except Exception as e:  # never break a user request because email failed
                log.status, log.error = "failed", str(e)[:290]
        db.session.commit()


def send_email(to, subject, html):
    log = EmailLog(to_email=to, subject=subject[:200], body=html, status="queued")
    db.session.add(log)
    db.session.commit()
    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        _deliver(app, log.id, to, subject, html)
    else:
        threading.Thread(target=_deliver, args=(app, log.id, to, subject, html), daemon=True).start()


def send_sms(phone, text):
    """SMS / WhatsApp via Twilio when TWILIO_* are set (prefix TWILIO_FROM with 'whatsapp:' for WhatsApp)."""
    cfg = current_app.config
    if not (cfg["TWILIO_SID"] and cfg["TWILIO_TOKEN"] and cfg["TWILIO_FROM"] and phone):
        return False
    try:
        import requests
        to = ("whatsapp:" if cfg["TWILIO_FROM"].startswith("whatsapp:") else "") + phone
        r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{cfg['TWILIO_SID']}/Messages.json",
                          data={"From": cfg["TWILIO_FROM"], "To": to, "Body": text},
                          auth=(cfg["TWILIO_SID"], cfg["TWILIO_TOKEN"]), timeout=10)
        return r.ok
    except Exception:
        return False


def notify(user, kind, title, body="", link=None, email=True, cta="View details", sms=False):
    """In-app notification + transactional email (+ optional SMS/WhatsApp)."""
    db.session.add(Notification(user_id=user.id, kind=kind, title=title, body=body, link=link))
    db.session.commit()
    if email:
        url = current_app.config["BASE_URL"] + link if link else None
        send_email(user.email, f"{title} — {current_app.config['GYM_NAME']}",
                   email_html(title, [body] if body else [], cta if url else None, url))
    if sms and user.phone:
        send_sms(user.phone, f"{title}. {body}"[:300])


def notify_admins(kind, title, body="", link="/admin"):
    admins = User.query.join(Role).filter(Role.name == "admin", User.status == "active").all()
    for a in admins:
        db.session.add(Notification(user_id=a.id, kind=kind, title=title, body=body, link=link))
    db.session.commit()
    to = {a.email for a in admins}
    if current_app.config["ADMIN_NOTIFY_EMAIL"]:
        to.add(current_app.config["ADMIN_NOTIFY_EMAIL"])
    for addr in to:
        send_email(addr, f"[Admin] {title}", email_html(title, [body] if body else [], "Open dashboard",
                                                       current_app.config["BASE_URL"] + link))
