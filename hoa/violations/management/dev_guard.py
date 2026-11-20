"""Safety stops for commands that only make sense on a test site, or that need care on the live one."""
import os

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

DEFAULT_PASSWORD = "demo-password-123"


def test_site_only(command):
    """Refuse on a live site (DJANGO_DEBUG not set to 1) unless ALLOW_DEV_COMMANDS=1 is set deliberately."""
    if settings.PRODUCTION and os.environ.get("ALLOW_DEV_COMMANDS") != "1":
        raise CommandError(f"'{command}' is for the test site only and is switched off on the live site, because it makes test "
                           "data or well-known passwords. (To run it on purpose, set ALLOW_DEV_COMMANDS=1 for this one command.)")


def real_password(password):
    """On the live site a login needs a password you chose: never the demo one, and strong enough to pass the site's own rules."""
    if not settings.PRODUCTION:
        return
    if password == DEFAULT_PASSWORD:
        raise CommandError("On the live site you must give a real password: add  --password \"a long password only you know\"")
    try:
        validate_password(password)
    except ValidationError as e:
        raise CommandError("That password is too weak: " + " ".join(e.messages))
