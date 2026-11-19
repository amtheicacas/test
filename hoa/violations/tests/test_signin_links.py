"""Owner sign-in by emailed link (no password needed), the optional password, and self-service password reset."""
import re
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from violations import accounts
from violations.models import LoginLink, Owner, Profile


@pytest.fixture
def owner(db):
    return Owner.objects.create(name="Pat Owner", property_address="1 Link Way", email="pat@example.com")


def request_link(client, email="pat@example.com"):
    mail.outbox.clear()
    client.post("/accounts/get-access/", {"email": email})
    return mail.outbox[0].body if mail.outbox else None


def test_owner_asks_for_a_link_opens_it_and_clicks_to_sign_in(client, owner):
    body = request_link(client)
    assert body and "expires in 15 minutes" in body
    path = re.search(r"/accounts/link/[^\s/]+/", body).group(0)
    r = client.get(path)                                   # an email scanner just opening the link...
    assert r.status_code == 200 and "Sign in" in r.content.decode() and "_auth_user_id" not in client.session
    r = client.post(path)                                  # ...does not sign in; the button does
    assert r.status_code == 302 and "_auth_user_id" in client.session
    assert client.get("/me/").status_code == 200
    assert client.session.get_expiry_age() >= 29 * 86400   # stays signed in for 30 days
    assert client.post(path).status_code == 410            # the link works once
    assert client.get(path).status_code == 410


def test_expired_link_is_refused(client, owner):
    body = request_link(client)
    path = re.search(r"/accounts/link/[^\s/]+/", body).group(0)
    LoginLink.objects.update(created_at=timezone.now() - timedelta(minutes=16))
    assert client.get(path).status_code == 410 and client.post(path).status_code == 410


def test_unknown_email_gets_the_same_answer_and_no_email(client, owner):
    mail.outbox.clear()
    r = client.post("/accounts/get-access/", {"email": "stranger@example.com"})
    assert r.status_code == 200 and not mail.outbox and "If that email address is on file" in r.content.decode()


def test_staff_are_not_given_a_link(client, owner, django_user_model):
    u = django_user_model.objects.create_user("boss@example.com", "boss@example.com", "pw")
    Profile.objects.create(user=u, role="manager")
    owner.additional_emails = "boss@example.com"; owner.save()
    mail.outbox.clear()
    client.post("/accounts/get-access/", {"email": "boss@example.com"})
    assert not mail.outbox


def test_when_the_email_changes_in_vantaca_only_the_new_one_works(client, owner):
    accounts.send_access_link("pat@example.com")           # creates the login
    owner.email = "pat.new@example.com"; owner.save()
    accounts.sync_account(owner)
    assert request_link(client, "pat@example.com") is None
    assert request_link(client, "pat.new@example.com")


def test_owner_can_set_a_password_and_use_it(client, owner):
    body = request_link(client)
    path = re.search(r"/accounts/link/[^\s/]+/", body).group(0)
    client.post(path)
    r = client.post("/me/password/", {"new_password1": "Correct-horse-9!", "new_password2": "Correct-horse-9!"})
    assert r.status_code == 302
    client.logout()
    r = client.post("/accounts/login/", {"username": "pat@example.com", "password": "Correct-horse-9!"})
    assert r.status_code == 302 and "_auth_user_id" in client.session


def test_owner_can_reset_their_own_password_by_email(client, owner):
    request_link(client)                                   # creates the login
    mail.outbox.clear()
    client.post("/accounts/password_reset/", {"email": "pat@example.com"})
    assert len(mail.outbox) == 1
    url = re.search(r"/accounts/reset/\S+?/\S+?/", mail.outbox[0].body).group(0)
    r = client.get(url, follow=True)
    r = client.post(r.request["PATH_INFO"], {"new_password1": "Another-long-pw-7!", "new_password2": "Another-long-pw-7!"})
    assert r.status_code == 302
    client.logout()
    r = client.post("/accounts/login/", {"username": "pat@example.com", "password": "Another-long-pw-7!"})
    assert r.status_code == 302 and "_auth_user_id" in client.session


def test_the_sign_in_page_leads_with_the_email_link(client):
    html = client.get("/accounts/login/").content.decode()
    assert "Email me a sign-in link" in html and "Sign in with a password instead" in html and "Forgot your password?" in html
