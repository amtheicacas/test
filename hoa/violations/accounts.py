"""Role helpers and homeowner login lifecycle (invite, keep in step with Vantaca, deactivate)."""
import secrets
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm

from .models import Profile

STAFF_ROLES = ("manager", "board")


def role_of(user):
    if not user.is_authenticated:
        return None
    p = getattr(user, "profile", None)
    if p is not None:
        return p.role
    return "manager" if (user.is_staff or user.is_superuser) else None


def owner_of(user):
    p = getattr(user, "profile", None)
    return p.owner if p and p.role == Profile.Role.HOMEOWNER else None


def _email_link(email, request=None):
    """Send Django's set/reset-password email (works with or without an HTTP request, e.g. from the sync job)."""
    form = PasswordResetForm({"email": email})
    if not form.is_valid():
        return
    if request is not None:
        form.save(request=request, use_https=request.is_secure(), from_email=None)
    else:
        u = urlparse(settings.SITE_URL)
        form.save(domain_override=u.netloc, use_https=u.scheme == "https", from_email=None)


def invite_owner(owner, request=None):
    """Create a homeowner login for the owner's email and email them a set-password link. Returns True if sent."""
    if not owner.email or not owner.active or owner.profiles.exists():
        return False
    User = get_user_model()
    if User.objects.filter(username__iexact=owner.email).exists():
        return False
    user = User.objects.create_user(owner.email, owner.email, secrets.token_urlsafe(24))  # usable but unknown
    Profile.objects.create(user=user, role=Profile.Role.HOMEOWNER, owner=owner)
    _email_link(owner.email, request)
    return True


def send_access_link(owner, request=None):
    """For the public 'get access' page: invite if no login yet, otherwise send a password-reset link."""
    if not invite_owner(owner, request):
        p = owner.profiles.select_related("user").first()
        if p and p.user.is_active:
            _email_link(p.user.email, request)


def sync_account(owner):
    """Keep the homeowner login in step with the owner record (email changes, owner left the community).
    Returns 'deactivated', 'updated' or None."""
    profile = owner.profiles.select_related("user").first()
    if not profile:
        return None
    user = profile.user
    if not owner.active:
        if user.is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])
            return "deactivated"
        return None
    changed = False
    if not user.is_active:  # owner is back on the roll
        user.is_active, changed = True, True
    new = owner.email
    User = get_user_model()
    if new and user.email.lower() != new.lower() and not User.objects.filter(username__iexact=new).exclude(pk=user.pk).exists():
        user.username = user.email = new  # login follows the Vantaca email; old email no longer works
        changed = True
    if changed:
        user.save()
        return "updated"
    return None
