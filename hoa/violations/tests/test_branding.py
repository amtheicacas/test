"""The website is called My Heron Landing (settings.APP_NAME); "Homeowner Portal" is Vantaca's site and is never used for this one."""
import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core import mail

from violations import accounts, pdf
from violations.models import Owner, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
U = get_user_model()
NAME = "My Heron Landing"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def test_the_name_is_a_setting_with_the_agreed_default(settings):
    assert settings.APP_NAME == NAME and settings.OTP_TOTP_ISSUER == NAME


def test_every_page_carries_the_name_in_the_title_and_header(client, settings):
    html = client.get("/accounts/login/").content.decode()
    assert f"· {NAME}</title>" in html
    assert f'<span class="appname">{NAME}</span><small>{settings.HOA_NAME}</small>' in html


def test_the_sign_in_page_welcomes_owners_to_it(client):
    html = client.get("/accounts/login/").content.decode()
    assert f"Welcome to {NAME}" in html and "Email me a sign-in link" in html


def test_changing_the_setting_changes_the_pages(client, settings):
    settings.APP_NAME = "Heron Landing Hub"
    html = client.get("/accounts/login/").content.decode()
    assert "Welcome to Heron Landing Hub" in html and NAME not in html


def test_the_sign_in_email_and_confirmation_page_use_the_name(client):
    Owner.objects.create(name="Pat", property_address="1 Brand St", email="pat@example.com")
    client.post("/accounts/get-access/", {"email": "pat@example.com"})
    m = mail.outbox[0]
    assert m.subject == f"Your {NAME} sign-in link" and f"sign in to {NAME}," in m.body and "portal" not in m.body.lower()
    page = client.get(re.search(r"/accounts/link/[^\s/]+/", m.body).group(0)).content.decode()
    assert f"sign in to {NAME}." in page


def test_the_password_reset_email_uses_the_name_and_on_the_live_site_the_real_address(client, settings):
    U.objects.create_user("pat@example.com", "pat@example.com", "Correct-horse-9!")
    settings.PRODUCTION, settings.SITE_URL, settings.ALLOWED_HOSTS = True, "https://hoa.example.com", ["testserver", "evil.example.net"]
    client.post("/accounts/password_reset/", {"email": "pat@example.com"}, HTTP_HOST="evil.example.net")
    m = mail.outbox[0]
    assert m.subject == f"Reset your {NAME} password" and f"password for {NAME}." in m.body
    assert "https://hoa.example.com/accounts/reset/" in m.body and "evil.example.net" not in m.body


def test_owner_home_page_names_it(client):
    o = Owner.objects.create(name="Pat", property_address="1 Brand St", email="pat@example.com")
    u = U.objects.create_user("p", "pat@example.com", "x")
    Profile.objects.create(user=u, role="homeowner", owner=o)
    client.force_login(u)
    assert f"Use {NAME} as your single place" in client.get("/me/").content.decode()


def test_letters_and_case_emails_send_owners_to_it():
    o = Owner.objects.create(name="Pat", property_address="1 Brand St", email="pat@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.get_or_create(name="Shed")[0], description="d", cure_required="c")
    assert f"through {NAME} at" in pdf._respond_line(v).text


def test_no_page_calls_this_site_a_portal():
    """The Homeowner Portal is Vantaca's. (Words like "portal_status" in code are fine; this checks the text owners read.)"""
    bad = re.compile(r"\b(owner|resident|homeowner|association|your|this) portal\b", re.I)
    hits = [f"{p.name}: {m.group(0)}" for p in TEMPLATES.rglob("*") if p.is_file() for m in bad.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []
