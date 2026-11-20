"""Phase 2 of the security review: photo type safety, limits on the private link, real visitor addresses behind a proxy, shared counters,
sign-in link lifecycle, links built from SITE_URL, and the password-reset limit."""
import importlib
import io
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory
from django.utils import timezone
from PIL import Image

from violations import accounts, throttle, uploads, views
from violations.models import LoginLink, Owner, Photo, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def picture(fmt="PNG", name="a.png"):
    b = io.BytesIO()
    Image.new("RGB", (30, 20), "green").save(b, fmt)
    return SimpleUploadedFile(name, b.getvalue())


def case(addr="1 Safe St", status=S.NOTICE_SENT, email=""):
    o = Owner.objects.create(name=f"Owner {addr}", property_address=addr, email=email)
    t = ViolationType.objects.get_or_create(name="Shed")[0]
    return Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=status)


def post_private(client, v, files=(), message="Fixed it"):
    return client.post(f"/r/{v.token}/", {"message": message, "photos": list(files)})


# ---------- SEC-01: a photo can only ever be a picture ----------
def test_a_picture_named_like_a_web_page_is_stored_and_served_as_a_picture(client):
    v = case()
    gif = io.BytesIO()
    Image.new("P", (4, 4)).save(gif, "GIF")
    sneaky = SimpleUploadedFile("evil.html", gif.getvalue() + b"<script>alert(document.domain)</script>")
    assert post_private(client, v, [sneaky]).status_code == 302
    p = Photo.objects.get(violation=v)
    assert p.image.name.endswith(".gif") and ".html" not in p.image.name
    r = client.get(f"/r/{v.token}/photo/{p.pk}/")
    assert r["Content-Type"] == "image/gif" and r["Content-Disposition"].startswith("inline")
    assert "sandbox" in r["Content-Security-Policy"] and r["X-Content-Type-Options"] == "nosniff"
    assert r["Referrer-Policy"] == "no-referrer"


def test_the_stored_type_follows_the_picture_not_the_name(client):
    v = case()
    post_private(client, v, [picture("PNG", "holiday.jpg"), picture("JPEG", "x.png")])
    assert sorted(p.image.name.rsplit(".", 1)[1] for p in Photo.objects.filter(violation=v)) == ["jpg", "png"]


def test_an_old_file_with_a_page_extension_is_only_ever_offered_as_a_download(client, settings):
    v = case()
    (settings.MEDIA_ROOT / "photos").mkdir()
    (settings.MEDIA_ROOT / "photos" / "legacy.html").write_bytes(b"<script>alert(1)</script>")
    p = Photo.objects.create(violation=v, image="photos/legacy.html")
    r = client.get(f"/r/{v.token}/photo/{p.pk}/")
    assert r["Content-Type"] == "application/octet-stream" and r["Content-Disposition"].startswith("attachment")


def test_photo_extensions_are_whitelisted():
    assert [uploads.safe_ext(n) for n in ("a.PNG", "b.html", "c", "d.svg", "e.jpeg", "f.webp")] == ["png", "jpg", "jpg", "jpg", "jpeg", "webp"]
    assert uploads.image_ext(SimpleUploadedFile("x.png", b"not a picture")) is None


def test_files_that_are_not_pictures_are_refused_with_a_message(client):
    v = case()
    r = client.post(f"/r/{v.token}/", {"message": "hi", "photos": [SimpleUploadedFile("notes.txt", b"hello"), picture()]}, follow=True)
    assert Photo.objects.filter(violation=v).count() == 1
    assert "notes.txt" in r.content.decode() and "not a JPG, PNG, GIF or WebP" in r.content.decode()


# ---------- SEC-12: limits on the private link ----------
def test_only_ten_photos_are_taken_at_a_time_and_the_rest_are_reported(client):
    v = case()
    r = client.post(f"/r/{v.token}/", {"message": "hi", "photos": [picture(name=f"{i}.png") for i in range(12)]}, follow=True)
    assert Photo.objects.filter(violation=v).count() == 10
    assert "only 10 photos can be added at a time" in r.content.decode()


def test_one_case_cannot_collect_unlimited_owner_photos(client, monkeypatch):
    v = case()
    monkeypatch.setattr(views, "MAX_RESIDENT_PHOTOS_PER_CASE", 2)
    post_private(client, v, [picture(name=f"{i}.png") for i in range(3)])
    post_private(client, v, [picture(name="again.png")])
    assert Photo.objects.filter(violation=v, from_resident=True).count() == 2


def test_oversized_pictures_and_oversized_batches_are_refused(client, monkeypatch):
    v = case()
    monkeypatch.setattr(views, "MAX_UPLOAD", 10)
    post_private(client, v, [picture()])
    assert Photo.objects.count() == 0
    monkeypatch.setattr(views, "MAX_UPLOAD", 10 * 1024 * 1024)
    monkeypatch.setattr(views, "MAX_UPLOAD_TOTAL", picture().size + 10)
    post_private(client, v, [picture(name="1.png"), picture(name="2.png")])
    assert Photo.objects.count() == 1


def test_the_private_link_cannot_be_used_to_flood_the_association(client):
    v = case()
    for i in range(views.RESIDENT_POSTS_PER_HOUR):
        assert post_private(client, v, message=f"note {i}").status_code == 302
    r = post_private(client, v, message="note that must be kept in the box")
    assert r.status_code == 429 and b"note that must be kept in the box" in r.content
    assert v.responses.count() == views.RESIDENT_POSTS_PER_HOUR


def test_the_private_page_does_not_pass_its_address_on(client):
    v = case()
    assert client.get(f"/r/{v.token}/")["Referrer-Policy"] == "no-referrer"


# ---------- SEC-03: the visitor's real address behind a proxy ----------
def test_client_address_comes_from_the_proxy_entry_not_from_what_the_visitor_claims(settings):
    rf = RequestFactory()
    req = rf.get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5")
    settings.TRUSTED_PROXY_COUNT = 1
    assert throttle.client_ip(req) == "203.0.113.5"             # the visitor's "6.6.6.6" is ignored
    settings.TRUSTED_PROXY_COUNT = 2
    assert throttle.client_ip(req) == "6.6.6.6"
    settings.TRUSTED_PROXY_COUNT = 0
    assert throttle.client_ip(req) == "10.0.0.1"                # your own PC: no proxy
    settings.TRUSTED_PROXY_COUNT = 1
    assert throttle.client_ip(rf.get("/", REMOTE_ADDR="10.0.0.1")) == "10.0.0.1"    # no header: fall back


def test_owners_behind_one_proxy_do_not_share_one_link_limit(client, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    for i in range(14):
        Owner.objects.create(name=f"O{i}", property_address=f"{i} Z St", email=f"o{i}@example.com")
    for i in range(14):
        client.post("/accounts/get-access/", {"email": f"o{i}@example.com"}, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR=f"198.51.100.{i}")
    assert len(mail.outbox) == 14
    mail.outbox.clear()
    for i in range(14):          # one visitor asking for many different people's links is still stopped
        client.post("/accounts/get-access/", {"email": f"o{i}@example.com"}, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.99")
    assert len(mail.outbox) == 10


def test_the_lockout_counts_each_visitor_separately_behind_a_proxy(client, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    U.objects.create_user("boss", "boss@example.com", "Correct-horse-9!")
    for _ in range(5):
        client.post("/accounts/login/", {"username": "boss", "password": "wrong"}, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.1")
    same = client.post("/accounts/login/", {"username": "boss", "password": "Correct-horse-9!"}, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.1")
    other = client.post("/accounts/login/", {"username": "boss", "password": "Correct-horse-9!"}, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.2")
    assert same.status_code == 429 and other.status_code == 302


def test_the_lockout_uses_the_same_address_helper():
    from django.conf import settings
    assert settings.AXES_CLIENT_IP_CALLABLE == "violations.throttle.client_ip"


# ---------- shared counters ----------
def test_counters_work_on_the_live_sites_database_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "django_cache"}}
    call_command("createcachetable", verbosity=0)
    assert [throttle.hit("t:one", limit=2) for _ in range(4)] == [False, False, True, True]


def test_a_plain_migrate_creates_the_cache_table(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "django_cache"}}
    assert "django_cache" not in connection.introspection.table_names()
    mig = importlib.import_module("violations.migrations.0039_throttle_cache_table")
    mig.create_cache_table(None, SimpleNamespace(connection=connection))
    assert "django_cache" in connection.introspection.table_names()


def test_a_broken_counter_store_never_locks_owners_out():
    broken = mock.Mock()
    broken.get.side_effect = RuntimeError("cache down")
    with mock.patch.object(throttle, "cache", broken):
        assert throttle.hit("x") is False


def test_the_live_site_keeps_its_counters_in_the_database():
    from django.core.exceptions import ImproperlyConfigured  # noqa: F401
    import subprocess, sys, os
    from violations.tests.test_deploy_safety import HOA_DIR, LIVE
    env = {**os.environ, **LIVE, "HOA_ENV_FILE": "no-such-file"}
    r = subprocess.run([sys.executable, "-c", "import config.settings as s; print(s.CACHES['default']['BACKEND'], s.TRUSTED_PROXY_COUNT)"],
                       cwd=HOA_DIR, env=env, capture_output=True, text=True)
    assert r.stdout.split() == ["django.core.cache.backends.db.DatabaseCache", "1"], r.stderr


# ---------- SEC-11: sign-in link lifecycle ----------
def link_path(body):
    return re.search(r"/accounts/link/[^\s/]+/", body).group(0)


def test_asking_again_cancels_the_earlier_link(client):
    Owner.objects.create(name="Pat", property_address="1 A St", email="pat@example.com")
    client.post("/accounts/get-access/", {"email": "pat@example.com"})
    first = link_path(mail.outbox[0].body)
    client.post("/accounts/get-access/", {"email": "pat@example.com"})
    second = link_path(mail.outbox[1].body)
    assert client.get(first).status_code == 410
    assert client.get(second).status_code == 200


def test_two_simultaneous_clicks_sign_in_once(client):
    from django.test import Client
    Owner.objects.create(name="Pat", property_address="1 A St", email="pat@example.com")
    client.post("/accounts/get-access/", {"email": "pat@example.com"})
    path = link_path(mail.outbox[0].body)
    ll = LoginLink.objects.get()
    other = Client()
    with mock.patch("violations.accounts.link_user", return_value=ll):         # both clicks saw the link as still unused
        a, b = client.post(path), other.post(path)
    assert sorted([a.status_code, b.status_code]) == [302, 410]
    assert ("_auth_user_id" in client.session) != ("_auth_user_id" in other.session)


def test_links_can_be_revoked_and_old_ones_are_cleared_daily():
    u = U.objects.create_user("p@example.com", "p@example.com", "x")
    accounts.send_signin_link(u)
    assert accounts.revoke_links(u) == 1 and accounts.revoke_links(u) == 0
    old = LoginLink.objects.create(user=u, token_hash="a" * 64)
    LoginLink.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
    fresh = LoginLink.objects.create(user=u, token_hash="b" * 64)
    call_command("daily_jobs")
    assert not LoginLink.objects.filter(pk=old.pk).exists() and LoginLink.objects.filter(pk=fresh.pk).exists()


# ---------- SEC-20: links are built from SITE_URL on the live site ----------
def test_live_site_links_use_site_url_even_if_the_request_names_another_host(client, settings):
    Owner.objects.create(name="Pat", property_address="1 A St", email="pat@example.com")
    settings.PRODUCTION, settings.SITE_URL, settings.ALLOWED_HOSTS = True, "https://hoa.example.com", ["testserver", "evil.example.net"]
    client.post("/accounts/get-access/", {"email": "pat@example.com"}, HTTP_HOST="evil.example.net")
    assert mail.outbox[0].body.count("https://hoa.example.com/accounts/link/") == 1 and "evil.example.net" not in mail.outbox[0].body


def test_test_site_links_follow_the_address_you_are_browsing(client, settings):
    Owner.objects.create(name="Pat", property_address="1 A St", email="pat@example.com")
    settings.PRODUCTION = False
    client.post("/accounts/get-access/", {"email": "pat@example.com"})
    assert "http://testserver/accounts/link/" in mail.outbox[0].body


# ---------- SEC-21: password-reset emails are limited ----------
def test_password_reset_cannot_be_used_to_flood_an_inbox(client):
    U.objects.create_user("pat@example.com", "pat@example.com", "Correct-horse-9!")
    results = []
    for _ in range(5):
        results.append(client.post("/accounts/password_reset/", {"email": "pat@example.com"}))
    unknown = client.post("/accounts/password_reset/", {"email": "nobody@example.com"})
    assert len(mail.outbox) == 3
    assert {r.status_code for r in results} == {302} and {r["Location"] for r in results + [unknown]} == {"/accounts/password_reset/done/"}


# ---------- a failing mail server must not reveal who has an account ----------
def test_a_mail_server_failure_looks_the_same_as_an_unknown_address(client):
    Owner.objects.create(name="Pat", property_address="1 A St", email="pat@example.com")
    with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("mail server down")):
        known = client.post("/accounts/get-access/", {"email": "pat@example.com"})
    unknown = client.post("/accounts/get-access/", {"email": "stranger@example.com"})
    assert known.status_code == unknown.status_code == 200 and b"we've sent" in known.content and b"we've sent" in unknown.content
