"""Role helpers and homeowner portal invitations."""
import secrets

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


def invite_owner(owner, request):
    """Create a homeowner login for the owner's email and email them a set-password link. Returns True if sent."""
    if not owner.email or owner.profiles.exists():
        return False
    User = get_user_model()
    if User.objects.filter(username__iexact=owner.email).exists():
        return False
    user = User.objects.create_user(owner.email, owner.email, secrets.token_urlsafe(24))  # usable but unknown
    Profile.objects.create(user=user, role=Profile.Role.HOMEOWNER, owner=owner)
    form = PasswordResetForm({"email": owner.email})
    if form.is_valid():
        form.save(request=request, use_https=request.is_secure(), from_email=None)
    return True
