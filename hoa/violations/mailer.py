"""Sending the sign-in link email without making the visitor wait for the mail server.

On the live site the email is handed to a background thread, so the sign-in page answers just as fast whether or not the address is on
file (a slower answer would tell a stranger which addresses belong to owners). In development it is sent at once, so tests and the
console email stay simple. A failure is logged, never shown to the visitor."""
import logging
import threading

from django.conf import settings

log = logging.getLogger(__name__)


def _send(message):
    try:
        message.send()
    except Exception:       # noqa: BLE001  (SMTP down, refused address...)
        log.exception("an email could not be sent")


def send(message):
    if settings.EMAIL_BACKGROUND:
        threading.Thread(target=_send, args=(message,), daemon=True).start()
    else:
        message.send()
