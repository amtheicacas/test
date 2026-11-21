"""Authenticator-app (TOTP) enrolment and verification, plus self-serve 'get access' for homeowners."""
import io

import qrcode
import qrcode.image.svg
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetCompleteView, PasswordResetConfirmView, PasswordResetDoneView, PasswordResetView
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

import logging
from urllib.parse import urlsplit

from . import accounts, throttle
from .models import LoginLink


log = logging.getLogger(__name__)


class CodeForm(forms.Form):
    code = forms.CharField(max_length=8, label="6-digit code", widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}))


def _next(request):
    n = request.GET.get("next") or request.POST.get("next") or "/"
    ok = n.startswith("/") and not n.startswith("//") and url_has_allowed_host_and_scheme(n, allowed_hosts={request.get_host()})     # a path on this site only (a bare word crashed the redirect)
    return n if ok else "/"


def _qr_svg(url):
    buf = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return buf.getvalue().decode()


@login_required
def security_setup(request):
    """Create/confirm an authenticator device. Replacing an existing device requires being verified first."""
    existing = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
    if existing and not request.user.is_verified():
        return redirect("security_verify")
    if existing:
        return render(request, "violations/security_enabled.html")
    device = TOTPDevice.objects.filter(user=request.user, confirmed=False).first() or \
        TOTPDevice.objects.create(user=request.user, name="authenticator", confirmed=False)
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["code"].replace(" ", "")):
            device.confirmed = True
            device.save()
            otp_login(request, device)
            messages.success(request, "Two-step login is on.")
            return redirect(_next(request))
        form.add_error("code", "That code didn't match. Check your phone's clock and try again.")
    return render(request, "violations/security_setup.html", {"form": form, "qr": _qr_svg(device.config_url),
                                                              "secret": device.config_url.split("secret=")[1].split("&")[0],
                                                              "next": _next(request)})


@login_required
def security_verify(request):
    device = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
    if not device:
        return redirect("security_setup")
    if request.user.is_verified():
        return redirect(_next(request))
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["code"].replace(" ", "")):
            otp_login(request, device)
            return redirect(_next(request))
        form.add_error("code", "Incorrect or expired code.")
    return render(request, "violations/security_verify.html", {"form": form, "next": _next(request)})


class AccessForm(forms.Form):
    email = forms.EmailField(label="Email address on file with the association")


def _throttled(key, limit=3, window=3600):
    return throttle.hit(key, limit, window)


def signin_link(request, token):
    """The page an emailed link opens. Signing in needs a click on the button (a POST), so an email scanner that merely
    opens the link can't use it up."""
    ll = accounts.link_user(token)
    if ll is None:
        return render(request, "violations/signin_link_invalid.html", status=410)
    if request.method == "POST":
        # claim the link in one step: if two clicks arrive together only one of them gets to use it
        if not LoginLink.objects.filter(pk=ll.pk, used_at__isnull=True).update(used_at=timezone.now()):
            return render(request, "violations/signin_link_invalid.html", status=410)
        auth_login(request, ll.user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("home_root")
    return render(request, "violations/signin_link_confirm.html", {"who": ll.user.first_name or "your account"})


def get_access(request):
    """Owner sign-in by emailed link. Only emails already on a Vantaca-synced owner record get a link,
    and the response is identical either way so it can't be used to discover who lives where."""
    form = AccessForm(request.POST or None)
    done = False
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        ip = throttle.client_ip(request)
        if not (_throttled(f"access:email:{email}") or _throttled(f"access:ip:{ip}", limit=10)):
            try:
                accounts.send_access_link(email, request)  # works for owner, co-owner and tenant emails on file
            except Exception:       # noqa: BLE001  (mail server down): the visitor sees the same page either way, so errors can't reveal who has an account
                log.exception("sign-in link could not be emailed")
        done = True
    return render(request, "violations/get_access.html", {"form": form, "done": done})


# The password-reset pages and email live under violations/ (not registration/): Django's admin app has pages with the same names
# and would otherwise be used instead, with its own wording and styling.
class ThrottledPasswordResetView(PasswordResetView):
    template_name = "violations/password_reset_form.html"
    email_template_name = "violations/password_reset_email.html"
    subject_template_name = "violations/password_reset_subject.txt"

    """Django's "forgot your password" page, with a limit on how many reset emails one address (or one visitor) can trigger, so it
    can't be used to flood someone's inbox. A refused request looks exactly like an accepted one."""

    def dispatch(self, request, *args, **kwargs):
        ctx = {"APP_NAME": settings.APP_NAME}
        if settings.PRODUCTION:       # on the live site the reset link always points at SITE_URL, never at whatever address the request named
            site = urlsplit(settings.SITE_URL)
            ctx.update(domain=site.netloc, protocol=site.scheme)
        self.extra_email_context = ctx
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data["email"].strip().lower()
        if _throttled(f"reset:email:{email}") or _throttled(f"reset:ip:{throttle.client_ip(self.request)}", limit=10):
            return HttpResponseRedirect(self.get_success_url())
        return super().form_valid(form)


class ResetDoneView(PasswordResetDoneView):
    template_name = "violations/password_reset_done.html"


class ResetConfirmView(PasswordResetConfirmView):
    template_name = "violations/password_reset_confirm.html"


class ResetCompleteView(PasswordResetCompleteView):
    template_name = "violations/password_reset_complete.html"
