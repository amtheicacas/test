import io
import time
import types
from datetime import date, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from PIL import Image

from violations import accounts, vantaca
from violations.models import Owner, Photo, Profile, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()
PW = "a-long-test-password-123"


@pytest.fixture(autouse=True)
def _clean(settings, tmp_path):
    cache.clear()
    settings.MEDIA_ROOT = tmp_path


def code(device):
    return f"{totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits):06d}"


@pytest.fixture
def one_code_step():
    """Hold django-otp's clock inside ONE 30-second code step. A test that needs the enrolment code and the later sign-in code to be the SAME
    code (the replay check) failed about one run in fifty, whenever the real clock crossed a step boundary half-way through it."""
    now = time.time()
    fixed = now - now % 30 + 1
    with mock.patch("django_otp.oath.time", lambda: fixed), mock.patch("django_otp.plugins.otp_totp.models.time", types.SimpleNamespace(time=lambda: fixed)):
        yield


def mk_user(role, owner=None, name=None):
    u = U.objects.create_user(name or role, f"{role}@x.com", PW)
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


# ---------- two-step login ----------
def test_privileged_roles_must_enrol_then_verify(client, settings, one_code_step):
    settings.MFA_REQUIRED_ROLES = ("manager", "board", "committee")
    mk_user("board")
    assert client.post("/accounts/login/", {"username": "board", "password": PW}).status_code == 302
    r = client.get("/violations/")  # not verified: bounced to enrolment
    assert r.status_code == 302 and r.url.startswith("/security/setup/")
    page = client.get("/security/setup/")
    assert page.status_code == 200 and b"<svg" in page.content
    dev = TOTPDevice.objects.get()
    assert not dev.confirmed
    assert b"didn" in client.post("/security/setup/", {"code": "000000"}).content  # wrong code rejected
    assert client.post("/security/setup/", {"code": code(dev)}).status_code == 200  # ...and retries are delayed
    TOTPDevice.objects.update(throttling_failure_count=0, throttling_failure_timestamp=None)  # wait out the delay
    assert client.post("/security/setup/", {"code": code(dev)}).status_code == 302
    assert client.get("/violations/").status_code == 200  # now verified
    # new session: password alone is no longer enough
    client.logout()
    client.post("/accounts/login/", {"username": "board", "password": PW})
    r = client.get("/violations/")
    assert r.status_code == 302 and r.url.startswith("/security/verify/")
    assert client.post("/security/verify/", {"code": "123456"}).status_code == 200  # wrong
    assert client.get("/violations/").status_code == 302
    TOTPDevice.objects.update(throttling_failure_count=0, throttling_failure_timestamp=None)
    dev = TOTPDevice.objects.get()
    assert client.post("/security/verify/", {"code": code(dev)}).status_code == 200  # same code as enrolment: replay refused
    TOTPDevice.objects.update(last_t=-1, throttling_failure_count=0, throttling_failure_timestamp=None)  # (a real user would simply wait for the next 30-second code)
    assert client.post("/security/verify/", {"code": code(dev)}).status_code == 302
    assert client.get("/violations/").status_code == 200


def test_homeowner_not_forced_but_can_opt_in(client, settings):
    settings.MFA_REQUIRED_ROLES = ("manager", "board", "committee")
    o = Owner.objects.create(name="Pat", property_address="1 A St")
    mk_user("homeowner", owner=o)
    client.post("/accounts/login/", {"username": "homeowner", "password": PW})
    assert client.get("/me/").status_code == 200
    dev = None
    client.get("/security/setup/")
    dev = TOTPDevice.objects.get()
    client.post("/security/setup/", {"code": code(dev)})
    client.logout()
    client.post("/accounts/login/", {"username": "homeowner", "password": PW})
    assert client.get("/me/").status_code == 302  # opted in, so now required


def test_open_redirect_blocked_on_verify(client, settings):
    settings.MFA_REQUIRED_ROLES = ("manager",)
    mk_user("manager")
    client.post("/accounts/login/", {"username": "manager", "password": PW})
    client.get("/security/setup/")
    dev = TOTPDevice.objects.get()
    r = client.post("/security/setup/", {"code": code(dev), "next": "https://evil.example/"})
    assert r.url == "/"


# ---------- lockout ----------
def test_account_locks_after_repeated_failures(client, settings):
    mk_user("manager")
    for _ in range(5):
        client.post("/accounts/login/", {"username": "manager", "password": "wrong"})
    r = client.post("/accounts/login/", {"username": "manager", "password": PW})  # even the right password
    assert r.status_code == 429 and "_auth_user_id" not in client.session


# ---------- self-serve access ----------
def test_get_access_only_emails_known_owners_and_looks_identical(client):
    Owner.objects.create(name="Pat", property_address="1 A St", email="pat@example.com")
    a = client.post("/accounts/get-access/", {"email": "pat@example.com"})
    b = client.post("/accounts/get-access/", {"email": "stranger@example.com"})
    assert a.status_code == b.status_code == 200
    assert b"we've sent" in a.content and b"we've sent" in b.content  # same answer either way
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["pat@example.com"]
    assert Profile.objects.get().role == "homeowner"


def test_get_access_is_throttled(client):
    Owner.objects.create(name="Pat", property_address="1 A St", email="pat@example.com")
    for _ in range(6):
        client.post("/accounts/get-access/", {"email": "pat@example.com"})
    assert len(mail.outbox) <= 3


def test_inactive_owner_gets_no_link(client):
    Owner.objects.create(name="Old", property_address="1 A St", email="old@example.com", active=False)
    client.post("/accounts/get-access/", {"email": "old@example.com"})
    assert mail.outbox == []


# ---------- vantaca keeps logins in step ----------
CSV = "Account ID,Homeowner Name,Property Address,Email Address\n{rows}\n"


def feed(*rows):
    return CSV.format(rows="\n".join(rows))


def test_sync_invites_new_owners_and_updates_login_email():
    vantaca.sync_csv(feed("A1,Pat,1 A St,pat@example.com", "A2,Sam,2 A St,"), invite=True)
    assert len(mail.outbox) == 1 and U.objects.get().username == "pat@example.com"  # Sam has no email: no login
    vantaca.sync_csv(feed("A1,Pat,1 A St,pat.new@example.com"))
    u = U.objects.get()
    assert u.username == "pat.new@example.com" and u.email == "pat.new@example.com"


def test_owner_missing_from_full_feed_loses_access():
    vantaca.sync_csv(feed(*[f"A{i},Owner{i},{i} A St,o{i}@example.com" for i in range(10)]), invite=True)
    r = vantaca.sync_csv(feed(*[f"A{i},Owner{i},{i} A St,o{i}@example.com" for i in range(1, 10)]), deactivate_missing=True)
    assert r["deactivated"] == 1
    gone = Owner.objects.get(external_id="A0")
    assert gone.active is False and not U.objects.get(username="o0@example.com").is_active
    # comes back on the next feed -> reactivated
    vantaca.sync_csv(feed(*[f"A{i},Owner{i},{i} A St,o{i}@example.com" for i in range(10)]))
    assert U.objects.get(username="o0@example.com").is_active


def test_partial_or_empty_feed_is_refused():
    vantaca.sync_csv(feed(*[f"A{i},Owner{i},{i} A St,o{i}@example.com" for i in range(10)]))
    with pytest.raises(vantaca.SafetyStop):
        vantaca.sync_csv(feed("A1,Owner1,1 A St,o1@example.com"), deactivate_missing=True)  # would drop 90%
    with pytest.raises(vantaca.SafetyStop):
        vantaca.sync_csv(feed(), deactivate_missing=True)
    assert Owner.objects.filter(active=True).count() == 10


def test_deactivated_homeowner_cannot_sign_in(client):
    o = Owner.objects.create(name="Pat", property_address="1 A St", email="p@example.com")
    mk_user("homeowner", owner=o, name="p@example.com")
    o.active = False
    o.save()
    accounts.sync_account(o)
    assert client.post("/accounts/login/", {"username": "p@example.com", "password": PW}).status_code == 200
    assert "_auth_user_id" not in client.session


# ---------- emailed link is limited to the cure period ----------
def _case(status):
    o = Owner.objects.create(name="Pat", property_address="1 A St", email="p@example.com")
    t = ViolationType.objects.create(name="Shed")
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=status,
                                 cure_deadline=date.today() + timedelta(days=5))
    b = io.BytesIO()
    Image.new("RGB", (9, 9)).save(b, "PNG")
    Photo.objects.create(violation=v, image=SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png"))
    return v


def test_private_link_works_in_cure_period_but_not_later(client):
    v = _case(S.NOTICE_SENT)
    assert b"Respond" in client.get(f"/r/{v.token}/").content
    v.status = S.HEARING_SCHEDULED
    v.save()
    page = client.get(f"/r/{v.token}/")
    assert b"Please sign in" in page.content and b"Respond" not in page.content
    assert client.post(f"/r/{v.token}/", {"message": "hi"}).status_code == 200
    assert v.responses.count() == 0
    assert client.get(f"/r/{v.token}/photo/{v.photos.first().pk}/").status_code == 404


def test_local_env_file_is_read_without_overriding_real_variables(tmp_path, monkeypatch):
    import importlib, os
    from config import settings as cfg
    (tmp_path / "local.env").write_text('# comment\nZOOM_TEST_A=one\nZOOM_TEST_B = "two"\nZOOM_TEST_C=three\n', encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path)
    monkeypatch.setenv("ZOOM_TEST_C", "from-the-real-environment")
    for k in ("ZOOM_TEST_A", "ZOOM_TEST_B"):
        monkeypatch.delenv(k, raising=False)
    cfg._load_local_env()
    assert os.environ["ZOOM_TEST_A"] == "one" and os.environ["ZOOM_TEST_B"] == "two" and os.environ["ZOOM_TEST_C"] == "from-the-real-environment"
    for k in ("ZOOM_TEST_A", "ZOOM_TEST_B"):
        os.environ.pop(k, None)
