"""`manage.py check_email`: the settings without any secret, a connection test, and ONE test message to ONE address, with each failure in plain words.
Everything runs against a fake mail server on this machine (violations/tests/fake_smtp.py); nothing leaves it."""
import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from violations.tests.fake_smtp import FakeSMTP

SECRET_USER, SECRET_PASSWORD = "user-token-abc123", "pw-token-xyz789"


@pytest.fixture
def smtp(settings):
    """Point the site at a fake mail server. Call it with FakeSMTP options; the settings carry (secret-looking) login details."""
    servers = []

    def make(login=(SECRET_USER, SECRET_PASSWORD), server_login=None, **options):
        srv = FakeSMTP(auth=server_login if server_login is not None else login, **options)
        servers.append(srv)
        settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        settings.EMAIL_HOST, settings.EMAIL_PORT = "127.0.0.1", srv.port
        settings.EMAIL_USE_TLS = settings.EMAIL_USE_SSL = False
        settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD = login or ("", "")
        settings.EMAIL_TIMEOUT = 3
        settings.DEFAULT_FROM_EMAIL = "hoa@heron-test.example"
        return srv
    yield make
    for s in servers:
        s.stop()


def run(*args):
    out = io.StringIO()
    try:
        call_command("check_email", *args, stdout=out)
    except CommandError as e:
        return out.getvalue(), str(e)
    return out.getvalue(), None


def test_without_to_it_shows_the_settings_tests_the_login_and_sends_nothing(smtp):
    srv = smtp()
    out, err = run()
    assert err is None and srv.accepted == []
    assert "REALLY sent" in out and f"127.0.0.1:{srv.port}" in out and "hoa@heron-test.example" in out
    assert f"user name set ({len(SECRET_USER)} characters)" in out and f"password set ({len(SECRET_PASSWORD)} characters)" in out
    assert "OK: connected and logged in. (Nothing was sent.)" in out and "Nothing was sent. To send ONE test message" in out


def test_with_to_it_sends_exactly_one_message_and_says_accepted_is_not_delivered(smtp):
    srv = smtp()
    out, err = run("--to", "board@example.com")
    assert err is None and len(srv.accepted) == 1
    mail = srv.accepted[0]
    assert mail["to"] == ["board@example.com"] and mail["from"] == "<hoa@heron-test.example>" and "test email" in mail["text"]
    assert "ACCEPTED" in out and "spam folder" in out and "Accepted is not the same as delivered" in out


def test_an_address_the_server_refuses_is_reported_with_the_servers_own_words(smtp):
    srv = smtp()
    out, err = run("--to", "nobody@example.com")
    assert srv.accepted == [] and "The test failed." in err and "refused the address you gave" in err and "nobody@example.com" in err and "User unknown" in err


def test_a_message_the_server_rejects_after_taking_it_is_reported(smtp):
    smtp(reject_data=True)
    _, err = run("--to", "board@example.com")
    assert "took the message but then rejected it" in err and "554" in err and "looks like spam" in err


def test_a_wrong_password_is_reported_before_anything_is_sent(smtp):
    srv = smtp(login=(SECRET_USER, "the-wrong-password"), server_login=(SECRET_USER, SECRET_PASSWORD))
    out, err = run("--to", "board@example.com")
    assert srv.accepted == [] and "Problems found" in err and "refused the user name or password" in err and "535" in err
    assert "Sending one test message" not in out


def test_a_from_address_the_service_has_not_verified_is_reported(smtp, settings):
    srv = smtp()
    settings.DEFAULT_FROM_EMAIL = "hoa@unverified.example"
    _, err = run("--to", "board@example.com")
    assert srv.accepted == [] and "refused the From address" in err and "unverified.example" in err and "verified" in err


def test_a_mail_server_that_is_down_is_reported_in_plain_words(smtp):
    srv = smtp()
    srv.stop()
    out, err = run("--to", "board@example.com")
    assert "Problems found" in err and "Could not connect to 127.0.0.1" in err and "EMAIL_HOST" in err and "Sending one test message" not in out


def test_the_placeholder_from_address_is_a_problem_when_emails_are_really_sent(smtp, settings):
    smtp()
    settings.DEFAULT_FROM_EMAIL = "violations@example.org"
    _, err = run()
    assert "still the placeholder" in err


def test_an_address_that_is_not_an_address_or_is_two_addresses_is_refused_before_anything_happens(smtp):
    srv = smtp()
    for bad in ("not-an-email", "a@example.com b@example.com", "a@example.com,b@example.com"):
        _, err = run("--to", bad)
        assert "does not look like an email address" in err and "Nothing was sent" in err
    assert srv.accepted == []


def test_on_a_site_that_only_prints_emails_nothing_real_is_sent(settings, capsys):
    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    settings.EMAIL_TO_LOG = True
    out, err = run("--to", "board@example.com")
    assert err is None and "only PRINTED, not sent" in out and "staging site" in out and "nothing real was sent" in out
    assert "test email" in capsys.readouterr().out                                            # the console backend printed the message itself


def test_no_secret_is_ever_printed_whatever_happens(smtp, settings):
    smtp(login=(SECRET_USER, "the-wrong-password"), server_login=(SECRET_USER, SECRET_PASSWORD))
    seen = [run(), run("--to", "board@example.com")]
    srv2 = smtp()
    seen += [run("--to", "board@example.com"), run("--to", "nobody@example.com")]
    srv2.stop()
    seen.append(run("--to", "board@example.com"))
    for out, err in seen:
        text = (out or "") + (err or "")
        for secret in (SECRET_USER, SECRET_PASSWORD, "the-wrong-password"):
            assert secret not in text
