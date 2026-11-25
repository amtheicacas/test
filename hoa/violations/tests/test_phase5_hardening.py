"""Phase 5: the smaller hardening items (SEC-16, 18, 19, 22-28)."""
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core import mail
from django.test import RequestFactory
from django.utils import timezone

from config import deploy
from violations import accounts, mailer, mailing, services, standards_search, vantaca
from violations.models import AuditLog, CannedReply, CommitteeMeeting, CommitteeMember, EmailLog, Notice, Owner, Photo, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
U = get_user_model()
HOA_DIR = Path(__file__).resolve().parents[2]
S = Violation.Status


def make_user(name, role, owner=None):
    u = U.objects.create_user(name, name if "@" in name else f"{name}@example.com", "x", first_name=name.title(), last_name="T")
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


@pytest.fixture
def w():
    oa = Owner.objects.create(name="Alex Owner", property_address="1 Case Way", email="alex@example.com")
    ob = Owner.objects.create(name="Bea Other", property_address="2 Other Way", email="bea@example.com")
    t = ViolationType.objects.create(name="Trash cans", default_cure_days=15)
    v = Violation.objects.create(owner=oa, vtype=t, description="d", cure_required="c", status=S.NOTICE_SENT)
    return type("W", (), dict(oa=oa, ob=ob, t=t, v=v, manager=make_user("mgr1", "manager"), board=make_user("board1", "board"),
                              committee=make_user("comm1", "committee"), home=make_user("alex@example.com", "homeowner", oa)))


def settings_run(env, code):
    """Load config.settings in a fresh process with the given environment and run `code` (exit status + output)."""
    e = {**os.environ, "DJANGO_DEBUG": "1", "HOA_ENV_FILE": "none", "DJANGO_SETTINGS_MODULE": "config.settings", **env}
    return subprocess.run([sys.executable, "-c", code], cwd=HOA_DIR, env=e, capture_output=True, text=True, timeout=60)


# ================= SEC-16: Vantaca must be https =================
@pytest.mark.parametrize("url", ["", "http://vantaca.example.com", "ftp://x.example.com", "vantaca.example.com", "https://", "http://localhost.evil.com"])
def test_a_non_https_vantaca_address_is_refused(settings, url):
    settings.VANTACA_BASE_URL, settings.DEBUG = url, False
    with pytest.raises(RuntimeError, match="https://"):
        vantaca.base_url()


def test_https_is_accepted_and_plain_http_only_for_this_computer_while_developing(settings):
    settings.VANTACA_BASE_URL, settings.DEBUG = "https://api.vantaca.example.com/", False
    assert vantaca.base_url() == "https://api.vantaca.example.com"
    settings.VANTACA_BASE_URL = "http://localhost:8001"
    with pytest.raises(RuntimeError):
        vantaca.base_url()
    settings.DEBUG = True
    assert vantaca.base_url() == "http://localhost:8001"
    settings.VANTACA_BASE_URL = "http://vantaca.example.com"
    with pytest.raises(RuntimeError):
        vantaca.base_url()


def test_no_request_is_made_to_a_plain_http_vantaca(settings):
    settings.VANTACA_BASE_URL, settings.VANTACA_USER, settings.VANTACA_PASSWORD = "http://vantaca.example.com", "u", "secret-pw"
    settings.VANTACA_OWNERS_PATH, settings.DEBUG = "/owners", False
    with mock.patch("urllib.request.urlopen") as net:
        with pytest.raises(RuntimeError, match="https"):
            vantaca.api_request("GET", "/x")
        with pytest.raises(RuntimeError, match="https"):
            vantaca.fetch_owners_from_api()
    assert net.call_count == 0


def test_the_live_site_will_not_start_with_a_plain_http_vantaca():
    good = dict(secret_key="k" * 50, allowed_hosts=["h.example.com"], site_url="https://h.example.com", database_url="postgres://u:p@h/db",
                email_host="smtp.example.com", from_email="v@heronlanding.org", mfa_roles=("manager",))
    assert deploy.production_problems(**good, vantaca_url="") == []
    assert deploy.production_problems(**good, vantaca_url="https://api.vantaca.example.com") == []
    assert any("VANTACA_BASE_URL" in p for p in deploy.production_problems(**good, vantaca_url="http://api.vantaca.example.com"))


# ================= SEC-18: email settings =================
def test_email_defaults_have_a_timeout_and_starttls():
    r = settings_run({"EMAIL_HOST": "smtp.example.com"}, "from django.conf import settings as s; print(s.EMAIL_TIMEOUT, s.EMAIL_PORT, s.EMAIL_USE_TLS, s.EMAIL_USE_SSL)")
    assert r.returncode == 0 and r.stdout.split() == ["10", "587", "True", "False"], r.stderr


def test_ssl_on_port_465_can_be_chosen_and_the_timeout_changed():
    r = settings_run({"EMAIL_HOST": "smtp.example.com", "EMAIL_USE_SSL": "1", "EMAIL_TIMEOUT": "20"},
                     "from django.conf import settings as s; print(s.EMAIL_TIMEOUT, s.EMAIL_PORT, s.EMAIL_USE_TLS, s.EMAIL_USE_SSL)")
    assert r.returncode == 0 and r.stdout.split() == ["20", "465", "False", "True"], r.stderr


def test_choosing_both_ssl_and_starttls_is_refused():
    r = settings_run({"EMAIL_HOST": "smtp.example.com", "EMAIL_USE_SSL": "1", "EMAIL_USE_TLS": "1"}, "from django.conf import settings as s; print(s.EMAIL_PORT)")
    assert r.returncode != 0 and "only one of EMAIL_USE_SSL" in r.stderr


def test_the_stored_copy_of_an_owner_email_does_not_keep_the_private_link(w, settings):
    mail.outbox.clear()
    services.send_notice(w.v, Notice.Kind.VIOLATION)
    token = w.v.token
    sent = [m for m in mail.outbox if "alex@example.com" in m.to][0]
    assert token in sent.body                                                        # the owner still gets a working link
    log = EmailLog.objects.filter(violation=w.v).latest("pk")
    assert token not in log.body and "[private link removed" in log.body and "Dear" in log.body


def test_other_owner_emails_are_redacted_too_and_the_setting_can_keep_them(w, settings):
    services._email_owner(w.v, "Reminder", "Please fix it.")
    assert w.v.token not in EmailLog.objects.filter(violation=w.v).latest("pk").body
    settings.EMAILLOG_REDACT_LINKS = False
    services._email_owner(w.v, "Reminder 2", "Please fix it.")
    assert w.v.token in EmailLog.objects.filter(violation=w.v).latest("pk").body


def test_redaction_leaves_ordinary_text_and_other_links_alone():
    body = "See https://site.example/r/short/ and https://example.org/page and /accounts/link/" + "A" * 40 + "/ ok"
    out = services.stored_copy(body)
    assert "https://example.org/page" in out and "/r/short/" in out and "A" * 40 not in out


# ================= SEC-19: sign-in mail is sent in the background on a live site =================
def test_background_sending_hands_the_email_to_a_thread(settings):
    settings.EMAIL_BACKGROUND = True
    mail.outbox.clear()
    mailer.send(mail.EmailMessage("s", "b", "from@example.com", ["to@example.com"]))
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=5)
    assert [m.to for m in mail.outbox] == [["to@example.com"]]


def test_a_failing_background_send_is_logged_not_raised(settings, caplog):
    settings.EMAIL_BACKGROUND = True
    msg = mail.EmailMessage("s", "b", "from@example.com", ["to@example.com"])
    with mock.patch.object(type(msg), "send", side_effect=OSError("smtp down")):
        mailer.send(msg)
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=5)
    assert "could not be sent" in caplog.text


def test_in_development_the_email_is_sent_at_once(settings):
    settings.EMAIL_BACKGROUND = False
    mail.outbox.clear()
    mailer.send(mail.EmailMessage("s", "b", "from@example.com", ["to@example.com"]))
    assert len(mail.outbox) == 1


def test_the_sign_in_page_answers_the_same_for_a_known_and_an_unknown_address(client, w, settings):
    settings.EMAIL_BACKGROUND = True
    a = client.post("/accounts/get-access/", {"email": "alex@example.com"})
    b = client.post("/accounts/get-access/", {"email": "nobody@example.com"})
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=5)
    assert a.status_code == b.status_code == 200
    assert ("sent" in a.content.decode().lower()) == ("sent" in b.content.decode().lower())


# ================= SEC-22: bad input is a message, never a server error =================
@pytest.mark.parametrize("junk", ["abc", "", "²", "1e9", "-5", "99999999999999999999999", "0x10", " "])
def test_junk_ids_and_years_on_the_roster_page_never_crash(client, w, junk):
    client.force_login(w.manager)
    for act in ("update", "delete", "add"):
        assert client.post("/committee/members/", {"action": act, "id": junk, "year": junk, "name": "X", "owner_pick": junk}).status_code in (200, 302)
    assert client.get("/committee/members/", {"year": junk}).status_code == 200


@pytest.mark.parametrize("junk", ["abc", "²", "-1", "99999999999999999999999"])
def test_junk_ids_on_other_staff_forms_never_crash(client, w, junk):
    client.force_login(w.manager)
    assert client.post("/replies/", {"action": "delete", "id": junk}).status_code == 302
    assert client.post("/replies/", {"action": "update", "id": junk, "title": "t", "body": "b"}).status_code == 302
    assert client.post("/owners/archive/", {"id": junk}).status_code in (302, 404)
    assert client.post("/meetings/assign-selected/", {"meeting": junk, "pick": [junk]}).status_code in (302, 404)
    assert client.post(f"/violations/{w.v.pk}/delete_photo/", {"photo": junk}).status_code == 404
    assert client.post(f"/violations/{w.v.pk}/mail_now/", {"notice": junk}).status_code == 404


def test_a_case_that_is_on_no_meeting_gives_a_message_not_an_error(client, w):
    client.force_login(w.manager)
    r = client.post(f"/committee/cases/{w.v.pk}/record/", {"outcome": "approve"}, follow=True)
    assert r.status_code == 200 and "not on a committee meeting" in r.content.decode()
    r = client.post(f"/results/cases/{w.v.pk}/review/", {"action": "approve"}, follow=True)
    assert r.status_code == 200 and "not on a committee meeting" in r.content.decode()


# ================= SEC-24: branded error pages, one answer for photos =================
def test_a_missing_page_shows_our_page(client, w):
    r = client.get("/no/such/page/")
    assert r.status_code == 404 and "We can't find that page" in r.content.decode()


def test_a_refused_page_shows_our_page(client, w):
    client.force_login(w.home)
    r = client.get("/audit/")
    assert r.status_code == 403 and "don't have access" in r.content.decode()


def test_the_error_page_renders_without_the_site(client):
    from django.views.defaults import server_error
    r = server_error(RequestFactory().get("/boom/"))
    assert r.status_code == 500 and b"Something went wrong" in r.content


def test_another_owners_photo_and_a_photo_that_does_not_exist_answer_the_same(client, w):
    from django.core.files.base import ContentFile
    p = Photo(violation=w.v)
    p.image.save("a.jpg", ContentFile(b"\xff\xd8\xff"), save=True)
    other = make_user("bea@example.com", "homeowner", w.ob)
    client.force_login(other)
    a, b = client.get(f"/photos/{p.pk}/"), client.get("/photos/999999/")
    assert a.status_code == b.status_code == 404
    strip = lambda r: [x for x in r.content.decode().splitlines() if "csrf" not in x.lower()]
    assert strip(a) == strip(b)                                       # same page, apart from the per-visit security token


# ================= SEC-23: the map's code is our own copy =================
def test_the_map_does_not_load_code_from_another_site(client, w):
    client.force_login(w.manager)
    page = client.get("/map/").content.decode()
    assert "cdnjs" not in page and "/static/violations/leaflet/leaflet.js" in page and "/static/violations/leaflet/leaflet.css" in page


def test_the_leaflet_files_are_in_the_project_and_are_version_1_9_4():
    for f in ("leaflet.js", "leaflet.css", "images/marker-icon.png", "images/layers.png", "LICENSE"):
        assert finders.find(f"violations/leaflet/{f}"), f
    assert "Leaflet 1.9.4" in Path(finders.find("violations/leaflet/leaflet.js")).read_text(encoding="utf-8")[:200]


# ================= SEC-25: Ask AI =================
def test_ask_ai_warns_not_to_type_names_or_addresses(client, w, settings):
    client.force_login(w.manager)
    with mock.patch.object(standards_search, "ai_available", return_value=True):
        page = client.get("/violations/new/").content.decode()
    assert "don't type owner names or addresses" in page


def test_ask_ai_has_a_daily_limit_for_the_team(client, w, settings):
    settings.AI_DAILY_LIMIT = 2
    client.force_login(w.manager)
    with mock.patch.object(standards_search, "ai_available", return_value=True), \
         mock.patch.object(standards_search, "ai_search", return_value=[]) as ai:
        for _ in range(2):
            assert client.get("/violations/standards/search/", {"q": "trash", "ai": "1"}).json()["ai"] is True
        r = client.get("/violations/standards/search/", {"q": "trash", "ai": "1"}).json()
    assert ai.call_count == 2 and r["ai"] is False and "daily limit" in r["ai_note"]


# ================= SEC-26: print-service fields =================
def test_form_fields_for_the_print_service_cannot_be_broken_by_quotes_or_line_breaks():
    body, ctype = mailing._multipart({"to[name]": 'Pat "The\r\nBoss" O\'Neil\x00', "to[address_line1]": "1 Main St\r\n--evil"}, "file", "notice.pdf", b"%PDF")
    text = body.decode("latin-1")
    assert 'Pat \'The Boss\' O\'Neil' in text and "\r\n--evil" not in text
    parts = [p for p in text.split("--" + ctype.split("boundary=")[1]) if "Content-Disposition" in p]
    assert len(parts) == 3                                                           # name, address, file: no injected extra part


# ================= SEC-27: no access log =================
def test_the_web_server_is_configured_without_an_access_log():
    ns = {}
    exec((HOA_DIR / "gunicorn.conf.py").read_text(encoding="utf-8"), ns)
    assert ns["accesslog"] is None


# ================= SEC-28: the same email on two households =================
def test_an_email_on_two_households_gets_no_link_and_is_flagged(client, w):
    w.ob.email = "alex@example.com"; w.ob.save()
    mail.outbox.clear()
    r = client.post("/accounts/get-access/", {"email": "alex@example.com"})
    assert r.status_code == 200 and not [m for m in mail.outbox if "alex@example.com" in m.to]
    e = AuditLog.objects.get(action="signin.ambiguous_email")
    assert "1 Case Way" in e.subject and "2 Other Way" in e.subject and "2 active households" in e.detail


def test_one_household_still_works_and_nothing_is_flagged(client, w):
    mail.outbox.clear()
    client.post("/accounts/get-access/", {"email": "alex@example.com"})
    assert [m for m in mail.outbox if "alex@example.com" in m.to] and not AuditLog.objects.exists()


def test_changing_a_login_to_an_address_that_two_households_list_is_refused(w):
    Owner.objects.create(name="C", property_address="3 C Way", email="dup@example.com")
    Owner.objects.create(name="D", property_address="4 D Way", email="dup@example.com")
    with pytest.raises(accounts.AccountError, match="different household"):
        accounts.change_signin_email(w.home.profile, "dup@example.com", w.manager, "r")


# ================= Render staging support =================
def test_the_health_check_answers_ok_for_any_host_without_redirecting(client, settings):
    settings.SECURE_SSL_REDIRECT = True
    r = client.get("/healthz", HTTP_HOST="10.0.0.7:10000")
    assert r.status_code == 200 and r.content == b"ok" and r["Cache-Control"] == "no-store"
    assert client.get("/healthz/", HTTP_HOST="evil.example.net").status_code == 200


def test_the_health_check_says_503_when_the_database_is_down(client):
    from django.db.backends.base.base import BaseDatabaseWrapper
    with mock.patch.object(BaseDatabaseWrapper, "cursor", side_effect=OSError("db down")):
        r = client.get("/healthz")
    assert r.status_code == 503 and b"database" in r.content


def test_the_health_check_needs_no_login_and_shows_nothing_else(client):
    assert client.get("/healthz").content == b"ok"


LIVE = {"DJANGO_DEBUG": "0", "SECRET_KEY": "k" * 50, "ALLOWED_HOSTS": "staging.example.com", "SITE_URL": "https://staging.example.com",
        "DATABASE_URL": "postgres://u:p@localhost/db", "DEFAULT_FROM_EMAIL": "violations@heronlanding.org", "STORAGE_BUCKET": "b"}


def test_a_live_site_without_email_is_still_refused():
    r = settings_run({**LIVE}, "from django.conf import settings as s; print(s.STAGING)")
    assert r.returncode != 0 and "EMAIL_HOST" in r.stderr


def test_a_staging_site_may_print_its_emails_in_the_log_instead():
    r = settings_run({**LIVE, "STAGING": "1", "EMAIL_TO_LOG": "1"}, "from django.conf import settings as s; print(s.STAGING, s.EMAIL_BACKEND)")
    assert r.returncode == 0 and r.stdout.split() == ["True", "django.core.mail.backends.console.EmailBackend"], r.stderr


def test_printing_emails_in_the_log_is_refused_on_a_site_that_is_not_staging():
    r = settings_run({**LIVE, "EMAIL_TO_LOG": "1"}, "from django.conf import settings as s; print(s.EMAIL_BACKEND)")
    assert r.returncode != 0 and "only allowed on a staging site" in r.stderr


def test_a_staging_site_shows_a_banner_and_every_page_asks_not_to_be_indexed(client, w, settings):
    client.force_login(w.manager)
    assert "STAGING SITE" not in client.get("/workspace/").content.decode()
    settings.STAGING = True
    page = client.get("/workspace/").content.decode()
    assert "STAGING SITE: test data only" in page and 'name="robots" content="noindex,nofollow"' in page


def test_the_staging_test_owner_file_imports_and_is_clearly_fake(w):
    text = (HOA_DIR / "staging-test-owners.csv").read_text(encoding="utf-8")
    result = vantaca.sync_csv(text)
    assert result["created"] == 6
    assert set(Owner.objects.filter(external_id__startswith="TEST").values_list("email", flat=True)) == {f"test.owner{i}@example.com" for i in range(1, 7)}


def test_the_build_script_installs_the_live_requirements_and_gathers_static_files():
    text = (HOA_DIR / "build.sh").read_text(encoding="utf-8")
    assert "requirements-production.lock.txt" in text and "collectstatic" in text and "errexit" in text and os.access(HOA_DIR / "build.sh", os.X_OK)


def test_the_pinned_lock_file_covers_every_live_package_with_exact_versions():
    """build.sh installs the lock file, so every package the live list names must be in it, pinned with ==."""
    import re
    names = lambda f: {re.split(r"[<>=\[ #;]", ln.strip(), maxsplit=1)[0].lower().replace("_", "-") for ln in (HOA_DIR / f).read_text(encoding="utf-8").splitlines()
                       if ln.strip() and not ln.startswith(("#", "-r"))}
    lock = [ln for ln in (HOA_DIR / "requirements-production.lock.txt").read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    assert all("==" in ln for ln in lock)
    locked = {ln.split("==")[0].lower().replace("_", "-") for ln in lock}
    wanted = names("requirements-production.txt") | names("requirements.txt")
    assert wanted <= locked, sorted(wanted - locked)
