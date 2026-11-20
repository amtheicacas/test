"""Hosting safety: the database address, the live-site startup checks, PostgreSQL-safe field sizes, and the guards on test-only commands."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from config import deploy
from violations import services, vantaca
from violations.models import Event, Owner, Violation, ViolationType

HOA_DIR = Path(__file__).resolve().parents[2]
GOOD = dict(secret_key="k" * 50, allowed_hosts=["hoa.example.com"], site_url="https://hoa.example.com", database_url="postgres://u:p@h/db",
            email_host="smtp.example.com", from_email="violations@heronlanding.org", mfa_roles=("manager", "board", "committee"))


# ---- DATABASE_URL ----
def test_database_url_is_read_into_a_postgres_entry():
    cfg = deploy.database_from_url("postgres://hoa_user:p%40ss%2Fword@dpg-123.render.internal:5432/hoa_db")
    assert cfg["ENGINE"] == "django.db.backends.postgresql"
    assert (cfg["NAME"], cfg["USER"], cfg["PASSWORD"], cfg["HOST"], cfg["PORT"]) == ("hoa_db", "hoa_user", "p@ss/word", "dpg-123.render.internal", "5432")
    assert cfg["OPTIONS"] == {} and cfg["CONN_MAX_AGE"] == 60 and cfg["CONN_HEALTH_CHECKS"] is True


def test_database_url_ssl_choices():
    assert deploy.database_from_url("postgresql://u:p@h/db", ssl_require=True)["OPTIONS"] == {"sslmode": "require"}
    assert deploy.database_from_url("postgres://u:p@h/db?sslmode=verify-full", ssl_require=True)["OPTIONS"] == {"sslmode": "verify-full"}


@pytest.mark.parametrize("bad", ["", "sqlite:///x.db", "mysql://u:p@h/db", "postgres://u:p@/db", "postgres://u:p@h/"])
def test_database_url_must_be_a_usable_postgres_address(bad):
    with pytest.raises(ValueError):
        deploy.database_from_url(bad)


# ---- the live-site checks ----
def test_good_live_settings_have_no_problems():
    assert deploy.production_problems(**GOOD) == []


@pytest.mark.parametrize("change, mention", [
    ({"secret_key": ""}, "SECRET_KEY"), ({"secret_key": deploy.DEV_SECRET}, "SECRET_KEY"), ({"secret_key": "short"}, "SECRET_KEY"),
    ({"secret_key": "django-insecure-" + "x" * 60}, "SECRET_KEY"),
    ({"allowed_hosts": []}, "ALLOWED_HOSTS"), ({"allowed_hosts": ["*"]}, "ALLOWED_HOSTS"),
    ({"site_url": "http://localhost:8000"}, "SITE_URL"), ({"database_url": ""}, "DATABASE_URL"),
    ({"email_host": ""}, "EMAIL_HOST"), ({"from_email": "violations@example.org"}, "DEFAULT_FROM_EMAIL"), ({"from_email": ""}, "DEFAULT_FROM_EMAIL"),
    ({"mfa_roles": ()}, "Two-step"),
])
def test_each_unsafe_live_setting_is_named(change, mention):
    problems = deploy.production_problems(**{**GOOD, **change})
    assert len(problems) == 1 and mention in problems[0]


def _load_settings(**extra):
    """Import config.settings in a fresh Python the way a live host would, with none of the developer's own settings."""
    env = {k: v for k, v in os.environ.items() if k not in (
        "DJANGO_DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "SITE_URL", "DATABASE_URL", "EMAIL_HOST", "DEFAULT_FROM_EMAIL", "MFA_REQUIRED_ROLES")}
    env.update(HOA_ENV_FILE="no-such-file", **extra)
    return subprocess.run([sys.executable, "-c",
                           "import config.settings as s; print('OK', s.DEBUG, s.PRODUCTION, s.DATABASES['default']['ENGINE'], "
                           "'whitenoise.middleware.WhiteNoiseMiddleware' in s.MIDDLEWARE, s.SECURE_HSTS_SECONDS if s.PRODUCTION else '-')"],
                          cwd=HOA_DIR, env=env, capture_output=True, text=True)


LIVE = dict(DJANGO_DEBUG="0", SECRET_KEY="k" * 50, ALLOWED_HOSTS="hoa.example.com", SITE_URL="https://hoa.example.com",
            DATABASE_URL="postgres://u:p@h:5432/db", EMAIL_HOST="smtp.example.com", DEFAULT_FROM_EMAIL="violations@heronlanding.org")


def test_a_site_with_no_settings_is_treated_as_live_and_refuses_to_start():
    r = _load_settings()
    assert r.returncode != 0 and "The site will not start" in r.stderr and "DJANGO_DEBUG=1" in r.stderr


def test_a_forgotten_variable_stops_the_live_site_with_a_plain_message():
    r = _load_settings(**{k: v for k, v in LIVE.items() if k != "EMAIL_HOST"})
    assert r.returncode != 0 and "EMAIL_HOST" in r.stderr and "SECRET_KEY" not in r.stderr


def test_two_step_login_cannot_be_switched_off_on_the_live_site():
    r = _load_settings(**LIVE, MFA_REQUIRED_ROLES="none")
    assert r.returncode != 0 and "Two-step" in r.stderr


def test_complete_live_settings_start_with_postgres_static_files_and_short_hsts():
    r = _load_settings(**LIVE)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["OK", "False", "True", "django.db.backends.postgresql", "True", "3600"]


def test_debug_on_your_own_pc_needs_nothing_else():
    r = _load_settings(DJANGO_DEBUG="1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[:5] == ["OK", "True", "False", "django.db.backends.sqlite3", "False"]


# ---- PostgreSQL-safe sizes ----
@pytest.fixture
def case(db):
    o = Owner.objects.create(name="Pat", property_address="1 Long Note Way")
    return Violation.objects.create(owner=o, vtype=ViolationType.objects.get_or_create(name="Shed")[0], description="d", cure_required="c")


def test_history_notes_have_no_length_limit(case):
    assert Event._meta.get_field("text").max_length is None      # PostgreSQL rejects text longer than a CharField's limit
    note = "A long board note. " * 100
    services.log(case, note)
    assert Event.objects.filter(violation=case).last().text == note


def test_typed_fields_stop_at_their_column_size_with_a_message_not_an_error_page():
    from violations import forms
    assert not forms.HoldForm({"reason": "x" * 301, "until": "2099-01-01"}).is_valid()
    assert forms.HoldForm({"reason": "x" * 300, "until": "2099-01-01"}).is_valid()
    assert not forms.MailedForm({"mailed_on": "2026-01-01", "tracking_number": "9" * 101}).is_valid()
    assert not forms.MeetingForm({"starts_at": "2099-01-01T10:00", "zoom_passcode": "p" * 41}).is_valid()


def test_import_fits_values_into_the_columns(db):
    csv = ("Account ID,Homeowner Name,Property Address,Email Address,Phone,State\n"
           f"A1,Pat Owner,12 Palm Way,pat@example.com,{'5' * 80},Florida\n"
           "A2,Sam Owner,14 Palm Way,sam@example.com,941-555-0100,Georgia\n"
           "A3,Kim Owner,16 Palm Way,kim@example.com,941-555-0101,Narnia\n")
    assert vantaca.sync_csv(csv)["created"] == 3
    pat, sam, kim = (Owner.objects.get(external_id=x) for x in ("A1", "A2", "A3"))
    assert pat.state == "FL" and len(pat.phone) == 50 and sam.state == "GA" and kim.state == ""


# ---- test-only commands ----
def test_test_commands_are_switched_off_on_a_live_site(settings, monkeypatch, db):
    settings.PRODUCTION = True
    monkeypatch.delenv("ALLOW_DEV_COMMANDS", raising=False)
    for cmd, args in (("seed_demo", []), ("make_test_owner", ["--email", "a@example.com"]), ("backdate_meeting", []), ("clear_test_data", ["--yes"])):
        with pytest.raises(CommandError, match="test site only"):
            call_command(cmd, *args)
    assert Violation.objects.count() == 0 and not Owner.objects.exists()


def test_test_commands_can_be_allowed_on_purpose(settings, monkeypatch):
    from violations.management import dev_guard
    settings.PRODUCTION = True
    monkeypatch.setenv("ALLOW_DEV_COMMANDS", "1")
    dev_guard.test_site_only("seed_demo")        # no error


def test_logins_made_on_a_live_site_need_a_real_password(settings, db):
    settings.PRODUCTION = True
    for pw in ("demo-password-123", "short"):
        with pytest.raises(CommandError):
            call_command("make_login", "board", "--username", "b1", "--password", pw)
    call_command("make_login", "board", "--username", "b1", "--password", "a-Long-Unusual-Password-91")
    from django.contrib.auth import get_user_model
    assert get_user_model().objects.get(username="b1").check_password("a-Long-Unusual-Password-91")


def test_test_site_keeps_its_easy_defaults(settings, db):
    settings.PRODUCTION = False
    call_command("make_login", "board", "--username", "b2")      # the demo password, as before
