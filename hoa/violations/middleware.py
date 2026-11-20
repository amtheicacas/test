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


class NoStorePrivatePages:
    """Pages shown to a signed-in person are never kept by the browser or a shared computer's cache, so the Back button after signing out
    cannot show someone's cases. A view that sets its own Cache-Control (the file downloads do) keeps it. Static files are untouched."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated and not request.path.startswith("/static/") and "Cache-Control" not in response:
            response["Cache-Control"] = "no-store, private"
        return response


class HealthCheck:
    """/healthz for the host's health check: answers "ok" if the website is running and can reach its database, 503 otherwise. It sits FIRST
    so it works whatever address or protocol the host's checker uses (it never looks at the Host header or redirects to https), and it
    reveals nothing but one word."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in ("/healthz", "/healthz/"):
            from django.db import connection
            from django.http import HttpResponse
            try:
                with connection.cursor() as c:
                    c.execute("SELECT 1")
                    c.fetchone()
            except Exception:       # noqa: BLE001
                return HttpResponse("database unavailable", status=503, content_type="text/plain")
            return HttpResponse("ok", content_type="text/plain", headers={"Cache-Control": "no-store"})
        return self.get_response(request)
