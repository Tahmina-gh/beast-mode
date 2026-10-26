# BEAST MODE — Gym Platform

Premium cinematic gym website **plus** a working platform behind it: accounts, memberships,
payments, class + trainer booking with waitlists, QR check-in, progress tracking, lead capture,
and member / trainer / admin dashboards. Flask + SQLAlchemy (SQLite locally, PostgreSQL in production).

## Windows quick start
Double-click **start.bat**. First run sets everything up (virtual env, packages, `.env`, admin account); every run after that just starts the site and opens your browser.

## Run it locally manually (5 minutes)

```bash
cd beast-platform
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # Windows: copy .env.example .env
python -c "import secrets;print(secrets.token_hex(32))"   # paste result into AUTH_SECRET in .env

flask --app app create-admin --email you@gym.com --password "ChooseAStrongOne1"
flask --app app seed-baseline        # 3 plans, FAQs, facilities, quiz rules (safe for production)
flask --app app seed-demo            # OPTIONAL fake members/trainers/classes for development only
python app.py                        # http://127.0.0.1:5000
```

Demo logins (after `seed-demo`, password `Demo12345`): `member1@demo.beastmode.test`,
`trainer1@demo.beastmode.test`. Remove all demo data any time: `flask --app app purge-demo`.

Without SMTP configured, emails are written to **Admin → Email log** (and the console) instead of sent,
and signup shows the verification link as a flash message in development.

## Tests
`pytest tests` — 22 tests: auth, RBAC, CSRF, XSS, uploads, capacity/waitlist, double-booking,
payments idempotency, refunds, expiry jobs, check-in.

## What's built (maps to the spec)
| Area | Status |
|---|---|
| Auth: signup, login, logout, forgot/reset (single-use), verify email, change password, profile + photo | Done |
| Google sign-in | **Not included** — needs your Google OAuth client ID; add `authlib` and a `/auth/google` route (the user model needs no change) |
| Roles: member / trainer / admin with server-side RBAC | Done |
| Member dashboard (membership, expiry, classes, PT, attendance, streak, payments, progress, notifications, QR) | Done |
| Plans CRUD, purchase, upgrade/downgrade/renew, cancel at period end, auto-expiry | Done |
| Payments: Stripe Checkout + signature-verified webhook, receipts, failed/refund flow, transaction IDs. No card data stored | Done (needs your Stripe keys) |
| Without Stripe keys: "pay at front desk" mode; admin marks paid -> everything activates | Done |
| Classes: create/filter/book/cancel, `12 / 20 booked`, atomic capacity guard, duplicates + clash prevention | Done |
| Waitlist: auto-offer to next person on cancellation, timed claim window, expiry -> next | Done |
| Trainer booking: profiles, availability, slots, plan-included sessions, double-booking impossible (DB unique lock) | Done |
| Free trial leads + status pipeline, tours (approve/reschedule/cancel/complete), contact inbox | Done |
| Attendance: QR + member ID, cooldown, check-out, daily/weekly/monthly | Done |
| Progress: weight, measurements, PRs, goals, charts; trainer/admin access only for their members | Done |
| Goal quiz with admin-editable rules | Done |
| Notifications (in-app + email) and reminders (30/7/1 day expiry, 24h class + session) | Done |
| SMS / WhatsApp | Architecture ready (Twilio via `TWILIO_*` env vars) |
| Admin: KPIs, charts, member management, bookings filters, content/media manager, testimonials, FAQs, analytics | Done |
| Security: hashing, CSRF, rate limits, server-side validation, secure uploads, CSP + security headers, no secrets in frontend | Done |

## Configuration
Everything is in `.env` (see `.env.example`). Key items:
- `AUTH_SECRET` — required in production; the app refuses to start without it.
- `DATABASE_URL` — `postgresql://...` in production (`pip install psycopg2-binary`).
- **Stripe:** set `PAYMENT_SECRET` and `STRIPE_WEBHOOK_SECRET`; add a webhook to
  `https://YOURDOMAIN/api/webhooks/stripe` for `checkout.session.completed` and `checkout.session.expired`.
- **Email:** `SMTP_HOST/PORT/USER`, `EMAIL_API_KEY`, `EMAIL_FROM` (works with Resend, SendGrid, Mailgun, Gmail app password).

## Scheduled jobs (important)
Expiry reminders, class reminders and waitlist expiry run via `flask --app app jobs`.
Locally `python app.py` runs them hourly automatically. In production schedule it hourly:
`0 * * * * cd /app && flask --app app jobs`

## Deploying
`Procfile` is included (`gunicorn "app:create_app()"`). Set `APP_ENV=production`, `COOKIE_SECURE=1`,
`TRUST_PROXY=1` behind a proxy, use PostgreSQL, and put uploads on a persistent disk (or swap
`save_upload` for S3). Rate limiting is in-memory per process — use a shared store (Redis) if you run many workers.

## Honest limits
- Money is processed only through Stripe Checkout; set real keys and test with Stripe test cards before launch.
- Terms page is placeholder text — replace with real terms/privacy policy.
- Demo photos hot-link Unsplash; upload your own in Admin → Website content.
