"""Force two-step login (authenticator app) for privileged roles, and for anyone who has turned it on."""
from django.conf import settings
from django.http import HttpResponseNotFound, QueryDict
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import user_has_device

from . import accounts

ALLOWED_PREFIXES = ("/security/", "/accounts/", "/static/")


def _without_nul(data):
    """The same form/link data with every NUL (0x00) character removed. Returns the SAME object when there is nothing to remove."""
    if not any("\x00" in k or any("\x00" in v for v in vs) for k, vs in data.lists()):
        return data
    clean = QueryDict(mutable=True, encoding=data.encoding)
    for k, vs in data.lists():
        for v in vs:
            clean.appendlist(k.replace("\x00", ""), v.replace("\x00", ""))
    clean._mutable = False       # like the original: a request's form data cannot be changed afterwards
    return clean


class StripNulCharacters:
    """PostgreSQL cannot store a NUL (0x00) character in text, so one typed or pasted into a form, a search box or a link (or sent by a
    program) ended in a server error on the live site; the test database (SQLite) accepts it, which hid the problem. Every NUL is removed from
    what is sent (form fields, search boxes, link parameters, in the website's own pages and the private owner links alike); a NUL in the
    address itself is simply "page not found". Nothing a person legitimately types contains one."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if "\x00" in request.path_info:
            return HttpResponseNotFound("Page not found", content_type="text/plain")
        request.GET = _without_nul(request.GET)
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method == "POST":                 # runs after Django's own check of the form's security code, which has already read the form
            request.POST = _without_nul(request.POST)


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
