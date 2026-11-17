"""Role helpers and homeowner login lifecycle (invite, keep in step with Vantaca, deactivate)."""
import secrets
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm

from .models import Owner, Profile

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


def household_logins(owner):
    return owner.profiles.select_related("user").order_by("pk")


def can_add_login(owner):
    return household_logins(owner).filter(user__is_active=True).count() < settings.MAX_LOGINS_PER_HOUSEHOLD


def invite_member(owner, email, name="", relationship="owner", request=None):
    """Give one more person a login on this household (everyone on an owner record sees the same cases and messages).
    Emails them a set-password link. Returns the Profile, or None if the email is blank/taken or the household is full."""
    email = (email or "").strip().lower()
    User = get_user_model()
    if not email or not owner.active or not can_add_login(owner):
        return None
    if User.objects.filter(username__iexact=email).exists():
        return None
    first, _, last = (name or "").strip().partition(" ")
    user = User.objects.create_user(email, email, secrets.token_urlsafe(24), first_name=first[:150], last_name=last[:150])
    profile = Profile.objects.create(user=user, role=Profile.Role.HOMEOWNER, owner=owner, relationship=relationship)
    _email_link(email, request)
    return profile


def invite_owner(owner, request=None):
    """Create the owner's login from the email on file (first login for a household). Returns True if sent."""
    if not owner.email or not owner.active or owner.profiles.exists():
        return False
    return invite_member(owner, owner.email, owner.display_name, "owner", request) is not None


def _relationship_for(owner, email):
    e = email.lower()
    if owner.email.lower() == e:
        return "owner"
    if e in {x.lower() for x in owner.tenant_emails}:
        return "tenant"
    return "co_owner" if e in {x.lower() for x in owner.all_emails} else "other"


def household_for_email(email):
    """The active owner record that lists this email (any owner email, the co-owner's or a tenant's), or None."""
    import re
    from django.db.models import Q
    pat = r"(^|[;,\s])" + re.escape(email) + r"($|[;,\s])"
    return Owner.objects.filter(Q(email__iexact=email) | Q(co_owner_email__iexact=email) | Q(tenant_email__iexact=email)
                                | Q(additional_emails__iregex=pat) | Q(tenant_additional_emails__iregex=pat), active=True).first()


def send_access_link(email, request=None):
    """Public 'get access' page: if the email is on a household record, create their login (or re-send a reset link)."""
    owner = household_for_email(email)
    if not owner:
        return False
    user = get_user_model().objects.filter(username__iexact=email).first() or get_user_model().objects.filter(email__iexact=email).first()
    if user:
        if user.is_active:
            _email_link(user.email, request)
        return True
    name = owner.tenant_name if _relationship_for(owner, email) == "tenant" else owner.display_name
    return invite_member(owner, email, name, _relationship_for(owner, email), request) is not None


def sync_account(owner):
    """Keep household logins in step with the owner record. Returns 'deactivated', 'updated' or None.
    If the owner left (inactive), every login in the household is switched off."""
    profiles = list(household_logins(owner))
    if not profiles:
        return None
    result = None
    for profile in profiles:
        user = profile.user
        if not owner.active:
            if user.is_active:
                user.is_active = False
                user.save(update_fields=["is_active"])
                result = "deactivated"
            continue
        if not user.is_active and profile.relationship == "owner":  # owner is back on the roll
            user.is_active = True
            user.save(update_fields=["is_active"])
            result = result or "updated"
    if owner.active:
        owner_profile = next((p for p in profiles if p.relationship == "owner"), None)
        new = owner.email
        User = get_user_model()
        if owner_profile and new and owner_profile.user.email.lower() != new.lower() \
                and not User.objects.filter(username__iexact=new).exclude(pk=owner_profile.user_id).exists():
            u = owner_profile.user
            u.username = u.email = new  # the owner's login follows the Vantaca email; the old address stops working
            u.save()
            result = "updated"
    return result
