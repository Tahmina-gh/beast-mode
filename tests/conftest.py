import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AUTH_SECRET", "test-secret-" + "x" * 32)

import pytest

from app import create_app
from models import db as _db


@pytest.fixture()
def app():
    application = create_app({
        "TESTING": True,
        "SMTP_HOST": "",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "RATELIMIT_ENABLED": False,
        "WTF_CSRF_ENABLED": True,
    })
    with application.app_context():
        _db.create_all()
        from seed import ensure_roles, seed_baseline
        ensure_roles()
        seed_baseline()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_meta(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def csrf_form(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def signup(client, email="user@example.com", password="Passw0rd1", name="Test User"):
    tok = csrf_form(client, "/signup")
    return client.post("/signup", data={"full_name": name, "email": email, "phone": "", "password": password,
                                        "password2": password, "terms": "on", "csrf_token": tok})


def login(client, email, password):
    tok = csrf_form(client, "/login")
    return client.post("/login", data={"email": email, "password": password, "csrf_token": tok})


def logout(client):
    client.post("/logout", data={"csrf_token": csrf_meta(client)})


def api_post(client, path, body=None):
    tok = csrf_meta(client)
    r = client.post(path, json=body or {}, headers={"X-CSRF-Token": tok, "X-Requested-With": "fetch"})
    return r, (r.get_json() if r.is_json else None)
