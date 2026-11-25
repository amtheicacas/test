"""Review pass 4 (R4-17): `manage.py check_email` shows the mail server's own words when something fails. A mail server can repeat the login it was given in those words
(with some services the "user name" IS the secret key), so the login is blanked out of everything the command prints. Every test here FAILED before the fix."""
import smtplib

import pytest

from violations.management.commands.check_email import explain

USER, PASSWORD = "user-token-abc123", "pw-token-xyz789"


@pytest.fixture(autouse=True)
def _login(settings):
    settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD = USER, PASSWORD
    settings.EMAIL_HOST, settings.EMAIL_PORT = "mail.example.org", 587


@pytest.mark.parametrize("exc", [
    smtplib.SMTPAuthenticationError(535, f"5.7.8 bad login for {USER} / {PASSWORD}".encode()),
    smtplib.SMTPResponseException(554, f"rejected (sender {USER})".encode()),
    smtplib.SMTPDataError(554, f"message from {PASSWORD} refused".encode()),
    smtplib.SMTPSenderRefused(550, f"not verified: {USER}".encode(), f"{USER}@example.com"),
    smtplib.SMTPRecipientsRefused({"a@example.com": (550, f"no such user {USER}".encode())}),
    smtplib.SMTPNotSupportedError(f"AUTH {USER}"),
    smtplib.SMTPServerDisconnected(f"closed after {PASSWORD}"),
    smtplib.SMTPException(f"odd: {USER}"),
    OSError(f"could not connect as {USER}:{PASSWORD}"),
    ValueError(f"surprise {PASSWORD}"),
])
def test_R4_17_the_mail_login_is_never_in_the_explanation(exc):
    text = explain(exc)
    assert USER not in text and PASSWORD not in text
    assert text                                                              # and something is still said


def test_R4_17_the_servers_own_words_are_still_shown():
    text = explain(smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication credentials invalid"))
    assert "535" in text and "Authentication credentials invalid" in text
