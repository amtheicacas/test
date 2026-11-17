"""Force two-step login (authenticator app) for privileged roles, and for anyone who has turned it on."""
from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import user_has_device

from . import accounts

ALLOWED_PREFIXES = ("/security/", "/accounts/", "/static/")


class RequireTwoStep:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        u = request.user
        if (u.is_authenticated and not u.is_verified() and not request.path.startswith(ALLOWED_PREFIXES)
                and (accounts.role_of(u) in settings.MFA_REQUIRED_ROLES or user_has_device(u))):
            target = "security_verify" if user_has_device(u) else "security_setup"
            return redirect(f"{reverse(target)}?next={request.get_full_path()}")
        return self.get_response(request)
