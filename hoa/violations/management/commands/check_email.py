import contextlib
import smtplib
import socket
import ssl

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.utils import timezone

PLACEHOLDER_FROM = "violations@example.org"          # the default in settings.py: not a real address


def _scrub(text):
    """Never print the mail login, even when the mail server's own error text repeats what it was given (with some mail services the "user name" IS the secret key)."""
    for secret in (getattr(settings, "EMAIL_HOST_USER", ""), getattr(settings, "EMAIL_HOST_PASSWORD", "")):
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    return text


def explain(exc):
    """A failure to talk to the mail server, in plain words, with the server's own text where it has one (the mail login is always blanked out of it)."""
    return _scrub(_explain(exc))


def _explain(exc):
    host, port = getattr(settings, "EMAIL_HOST", ""), getattr(settings, "EMAIL_PORT", "")
    where = f"{host}:{port}" if host else "the mail server"
    server_text = ""
    if isinstance(exc, smtplib.SMTPResponseException):
        raw = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        server_text = f" The server said: {exc.smtp_code} {raw}".rstrip()
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("The mail server refused the user name or password (EMAIL_HOST_USER / EMAIL_HOST_PASSWORD). Check they were copied whole, "
                "with no space at either end." + server_text)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (f"The mail server refused the From address ({exc.sender}). Mail services only send from an address or domain you have verified "
                f"with them: check DEFAULT_FROM_EMAIL and the service's list of verified senders." + server_text)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        parts = [f"{addr}: {code} {(msg.decode('utf-8', 'replace') if isinstance(msg, bytes) else msg)}" for addr, (code, msg) in exc.recipients.items()]
        return "The mail server refused the address you gave (it does not accept mail for it): " + "; ".join(parts)
    if isinstance(exc, smtplib.SMTPDataError):
        return "The mail server took the message but then rejected it." + server_text
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return f"The mail server does not support what the settings ask for ({exc}). Check EMAIL_USE_TLS / EMAIL_USE_SSL and EMAIL_PORT."
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return (f"The secure connection to {where} failed ({type(exc).__name__}). Port 587 normally needs EMAIL_USE_TLS=1 and port 465 needs "
                "EMAIL_USE_SSL=1 (not both): check EMAIL_PORT and those two settings.")
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return (f"{where} closed the connection without answering. This usually means the wrong port or the wrong secure-connection setting "
                "(EMAIL_PORT, EMAIL_USE_TLS, EMAIL_USE_SSL).")
    if isinstance(exc, socket.gaierror):
        return f"The name of the mail server ({host or 'EMAIL_HOST'}) could not be found. Check how EMAIL_HOST is spelled."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"{where} did not answer within {settings.EMAIL_TIMEOUT} seconds. Check EMAIL_HOST and EMAIL_PORT, and that the mail service is up."
    if isinstance(exc, (ConnectionRefusedError, smtplib.SMTPConnectError, OSError)):
        return f"Could not connect to {where} ({type(exc).__name__}: {exc}). Check EMAIL_HOST and EMAIL_PORT, and that the mail service is up."
    if isinstance(exc, smtplib.SMTPException):
        return f"The mail server reported a problem ({type(exc).__name__}).{server_text}"
    return f"Unexpected problem: {type(exc).__name__}: {exc}"


class Command(BaseCommand):
    help = ("Check how this site sends email, and (with --to) send ONE test message to ONE address you type. Without --to nothing is sent: it shows the "
            "settings (never a password or a user name, only whether they are set) and tests the connection and login. A passed test means the mail "
            "server ACCEPTED the message; it does not prove the message reached an inbox.")

    def add_arguments(self, p):
        p.add_argument("--to", help="the one address to send a test message to (omit it to check the settings and the connection only)")

    def handle(self, *a, **o):
        out = self.stdout.write
        to = (o.get("to") or "").strip()
        if to:
            try:
                validate_email(to)
            except ValidationError:
                raise CommandError(f"'{to}' does not look like an email address. Nothing was sent.") from None
        is_smtp = settings.EMAIL_BACKEND.endswith("smtp.EmailBackend")
        is_console = settings.EMAIL_BACKEND.endswith("console.EmailBackend")
        bad = []
        out("Email settings (no password or user name is ever shown):")
        if is_smtp:
            security = "SSL" if settings.EMAIL_USE_SSL else "STARTTLS" if settings.EMAIL_USE_TLS else "none (not secure)"
            out(f"  Emails are REALLY sent, through {settings.EMAIL_HOST}:{settings.EMAIL_PORT} (secure connection: {security}).")
            user, pw = settings.EMAIL_HOST_USER or "", settings.EMAIL_HOST_PASSWORD or ""
            out(f"  Login to the mail server: user name {'set (%d characters)' % len(user) if user else 'NOT set'}, "
                f"password {'set (%d characters)' % len(pw) if pw else 'NOT set'}.")
            if security.startswith("none"):
                out("  Note: the connection is not encrypted. Mail services normally need EMAIL_USE_TLS=1 (port 587) or EMAIL_USE_SSL=1 (port 465).")
        elif is_console:
            why = "this is a staging site (EMAIL_TO_LOG)" if settings.EMAIL_TO_LOG else "no EMAIL_HOST is set"
            out(f"  Emails are only PRINTED, not sent ({why}). Nothing real can be sent from this site.")
        else:
            out(f"  Email backend: {settings.EMAIL_BACKEND} (not the normal one).")
        out(f"  From address on every email: {settings.DEFAULT_FROM_EMAIL}")
        if is_smtp and settings.DEFAULT_FROM_EMAIL.strip().lower() == PLACEHOLDER_FROM:
            bad.append("DEFAULT_FROM_EMAIL is still the placeholder (violations@example.org). Set it to an address on a domain the mail service has verified.")
        out(f"  Waits at most {settings.EMAIL_TIMEOUT} seconds for the mail server.")

        if is_smtp:
            out("Connecting to the mail server and logging in ...")
            connection = get_connection(fail_silently=False)
            try:
                connection.open()
            except Exception as e:      # noqa: BLE001  (whatever it is, say it in plain words)
                bad.append(explain(e))
            else:
                out("  OK: connected and logged in. (Nothing was sent.)")
            finally:
                with contextlib.suppress(Exception):      # closing a connection that never opened must not hide the real problem
                    connection.close()
        if bad:
            raise CommandError("Problems found:\n  - " + "\n  - ".join(bad))

        if not to:
            out("Nothing was sent. To send ONE test message: python manage.py check_email --to you@example.com")
            return
        message = EmailMessage(
            f"{settings.APP_NAME} test email",
            f"This is a test email from the {settings.APP_NAME} ({settings.HOA_NAME}), sent on {timezone.localdate():%b} {timezone.localdate().day}, "
            f"{timezone.localdate().year}.\n\nIf you can read it, email sending works. Nothing needs to be done.\n",
            settings.DEFAULT_FROM_EMAIL, [to])
        out(f"Sending one test message to {to} ...")
        try:
            message.send()
        except Exception as e:      # noqa: BLE001
            raise CommandError("The test failed.\n  - " + explain(e)) from None
        if is_smtp:
            out("  The mail server ACCEPTED the message.")
            out("Now look in the inbox of that address (and its spam folder): it should arrive within a minute or two.")
            out("Accepted is not the same as delivered. If it never arrives, the mail service took it but could not deliver it: look at the mail "
                "service's own activity page for the reason.")
        else:
            out("  (Printed above: nothing real was sent.)")
