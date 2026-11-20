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
        # an owner who also holds a committee/Board/manager role needs the two-step code only for that role's pages, not for their own violations
        own_area = (u.is_authenticated and accounts.owner_of(u) is not None and accounts.role_of(u) != "homeowner"
                    and (request.path == "/" or request.path.startswith("/me/")))
        if (u.is_authenticated and not u.is_verified() and not request.path.startswith(ALLOWED_PREFIXES) and not own_area
                and (accounts.role_of(u) in settings.MFA_REQUIRED_ROLES or user_has_device(u))):
            target = "security_verify" if user_has_device(u) else "security_setup"
            return redirect(f"{reverse(target)}?next={request.get_full_path()}")
        return self.get_response(request)
