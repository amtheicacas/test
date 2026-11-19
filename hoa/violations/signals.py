from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from . import accounts


@receiver(user_logged_in)
def long_owner_sessions(sender, request, user, **kwargs):
    """Owners stay signed in on their device for OWNER_SESSION_DAYS; staff keep the shorter default session."""
    if request is not None and accounts.role_of(user) == "homeowner":
        request.session.set_expiry(accounts.OWNER_SESSION_DAYS * 86400)
