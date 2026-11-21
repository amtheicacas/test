"""Safety checks around the print service (Lob): which key is set, staging can never use a live key, the live site can never silently use a test
key, how the return address prints, and a `check_lob` command that sends nothing. (Lob has never run live: Lob's rules come from its published docs.)"""
import io
import urllib.error
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from config import deploy
from violations import mailing
from violations.models import MailPiece, Notice, Owner, Violation, ViolationType
from violations.tests.test_certified_mail import ADDR, _FakeResponse

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def lob_settings(settings):
    settings.MAIL_PROVIDER, settings.STAGING, settings.PRODUCTION = "lob", False, False
    settings.LOB_API_KEY, settings.HOA_RETURN_NAME = "test_abc123", ""
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_CITY, settings.HOA_RETURN_STATE, settings.HOA_RETURN_ZIP = "100 Main St #5", "Sarasota", "FL", "34236"


def run(*args):
    out = io.StringIO()
    call_command("check_lob", *args, stdout=out)
    return out.getvalue()


# ---------- which kind of key ----------
@pytest.mark.parametrize("key,mode", [("test_abc", "test"), ("live_abc", "live"), ("", "missing"), ("   ", "missing"), ("abc", "unknown"), ("TEST_abc", "unknown")])
def test_the_kind_of_lob_key_is_recognised(key, mode):
    assert mailing.lob_key_mode(key) == mode


def test_a_send_with_no_key_or_a_live_key_on_a_test_copy_is_refused_before_anything_leaves_the_server(settings):
    called = mock.Mock()
    with mock.patch("urllib.request.urlopen", called):
        settings.LOB_API_KEY = ""
        with pytest.raises(RuntimeError, match="not set"):
            mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
        for staging, production in ((True, True), (False, False)):          # staging, and a copy on someone's PC
            settings.STAGING, settings.PRODUCTION, settings.LOB_API_KEY = staging, production, "live_abc"
            with pytest.raises(RuntimeError, match="TEST key"):
                mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
    assert not called.called


def test_a_test_key_anywhere_and_a_live_key_on_the_live_site_still_send(settings):
    for staging, production, key in ((True, True, "test_abc"), (False, False, "test_abc"), (False, True, "live_abc")):
        settings.STAGING, settings.PRODUCTION, settings.LOB_API_KEY = staging, production, key
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _FakeResponse({"id": "ltr_1"})):
            assert mailing.lob_send(ADDR, b"%PDF-1", "Case 1")["id"] == "ltr_1"


def test_a_notice_on_staging_with_a_live_key_is_not_mailed_and_says_why(settings):
    settings.STAGING, settings.PRODUCTION, settings.LOB_API_KEY = True, True, "live_abc"
    o = Owner.objects.create(name="Pat", property_address="1 Palm Way", city="Sarasota", state="FL", zip_code="34240", email="p@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T"), description="d", cure_required="c", status="hearing_scheduled")
    n = Notice.objects.create(violation=v, kind="hearing")
    with mock.patch("urllib.request.urlopen") as net:
        assert mailing.dispatch(n, lambda who: b"%PDF-1") is False
    n.refresh_from_db()
    assert not net.called and not MailPiece.objects.exists() and "TEST key" in n.provider_note


# ---------- the live site's start checks ----------
GOOD = dict(secret_key="x" * 50, allowed_hosts=["hoa.example.com"], site_url="https://hoa.example.com", database_url="postgres://u@h/d",
            email_host="smtp.example.com", from_email="hoa@example.com", mfa_roles=["board"])


@pytest.mark.parametrize("change,fragment", [
    (dict(mail_provider="lob", lob_key="test_abc"), "TEST key"),                       # a live site with a test key would mail nothing
    (dict(mail_provider="lob", lob_key="live_abc", staging=True), "never a live key"),  # staging with a live key would mail for real
    (dict(mail_provider="lob", lob_key=""), "LOB_API_KEY"),
])
def test_the_live_site_refuses_to_start_with_an_unsafe_lob_setting(change, fragment):
    problems = deploy.production_problems(**GOOD, **change)
    assert any(fragment in p for p in problems), problems


@pytest.mark.parametrize("change", [dict(), dict(mail_provider="manual", lob_key="test_abc"), dict(mail_provider="lob", lob_key="live_abc"),
                                    dict(mail_provider="lob", lob_key="test_abc", staging=True)])
def test_safe_lob_settings_pass_the_start_checks(change):
    assert deploy.production_problems(**GOOD, **change) == []


# ---------- how the return address prints ----------
def test_the_associations_long_name_is_cut_at_a_word_and_plain_quotes_are_used(settings):
    assert mailing.return_name() == "Heron Landing Homeowners’ Association"           # 43 characters cut at a word, trailing comma dropped
    seen = {}

    def fake(req, timeout=None):
        seen["body"] = req.data.decode("utf-8", "replace")
        return _FakeResponse({"id": "ltr_1"})
    with mock.patch("urllib.request.urlopen", fake):
        mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
    assert "from[name]\"\r\n\r\nHeron Landing Homeowners' Association\r\n" in seen["body"]           # curly apostrophe made plain
    settings.HOA_RETURN_NAME = "Heron Landing HOA"
    assert mailing.return_name() == "Heron Landing HOA"


# ---------- the check_lob command ----------
def test_check_lob_says_what_the_key_does_and_never_prints_it(settings):
    text = run()
    assert "TEST key" in text and "NOTHING IS MAILED" in text and "test_abc123" not in text and "11 characters" in text
    assert "100 Main St #5" in text and "Sarasota, FL 34236" in text and "All checks passed" in text and "cut to Lob" in text
    settings.LOB_API_KEY, settings.PRODUCTION = "live_secretvalue", True                  # a live key is only acceptable on the live site
    text = run()
    assert "LIVE key" in text and "REALLY printed" in text and "secretvalue" not in text


def test_check_lob_fails_loudly_for_the_unsafe_cases(settings):
    settings.STAGING, settings.PRODUCTION, settings.LOB_API_KEY = True, True, "live_abc"
    with pytest.raises(CommandError, match="may only use a Lob TEST key"):
        run()
    settings.STAGING, settings.PRODUCTION = False, False                                  # a copy on a PC
    with pytest.raises(CommandError, match="may only use a Lob TEST key"):
        run()
    settings.STAGING, settings.PRODUCTION, settings.LOB_API_KEY = False, True, "test_abc"
    with pytest.raises(CommandError, match="TEST key"):
        run()
    settings.PRODUCTION, settings.LOB_API_KEY = False, ""
    with pytest.raises(CommandError, match="not set"):
        run()
    settings.LOB_API_KEY, settings.HOA_RETURN_LINE1 = "test_abc", ""
    with pytest.raises(CommandError, match="return address is incomplete"):
        run()


def test_check_lob_ping_asks_lob_read_only_and_reports_the_answer():
    seen = {}

    def ok(req, timeout=None):
        seen["url"], seen["method"] = req.full_url, req.get_method()
        return mock.MagicMock(__enter__=lambda s: mock.Mock(status=200), __exit__=lambda *a: False)
    with mock.patch("urllib.request.urlopen", ok):
        text = run("--ping")
    assert "accepts the key" in text and seen["method"] == "GET" and seen["url"].startswith("https://api.lob.com/v1/addresses")
    refused = urllib.error.HTTPError("https://api.lob.com/v1/addresses", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
    with mock.patch("urllib.request.urlopen", side_effect=refused):
        with pytest.raises(CommandError, match="HTTP 401"):
            run("--ping")
    with mock.patch("urllib.request.urlopen", side_effect=OSError("no network")):
        with pytest.raises(CommandError, match="Could not reach Lob"):
            run("--ping")


def test_check_lob_without_ping_makes_no_network_call():
    with mock.patch("urllib.request.urlopen") as net:
        run()
    assert not net.called
