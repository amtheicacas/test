"""Authenticator-app (TOTP) enrolment and verification, plus self-serve 'get access' for homeowners."""
import io

import qrcode
import qrcode.image.svg
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from . import accounts
from .models import Owner


class CodeForm(forms.Form):
    code = forms.CharField(max_length=8, label="6-digit code", widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}))


def _next(request):
    n = request.GET.get("next") or request.POST.get("next") or "/"
    return n if url_has_allowed_host_and_scheme(n, allowed_hosts={request.get_host()}) else "/"


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
    n = cache.get(key, 0)
    cache.set(key, n + 1, window)
    return n >= limit


def get_access(request):
    """Self-serve sign-up / password reset. Only emails already on a Vantaca-synced owner record get a link,
    and the response is identical either way so it can't be used to discover who lives where."""
    form = AccessForm(request.POST or None)
    done = False
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        ip = request.META.get("REMOTE_ADDR", "")
        if not (_throttled(f"access:email:{email}") or _throttled(f"access:ip:{ip}", limit=10)):
            owner = Owner.objects.filter(email__iexact=email, active=True).first()
            if owner:
                accounts.send_access_link(owner, request)
        done = True
    return render(request, "violations/get_access.html", {"form": form, "done": done})
