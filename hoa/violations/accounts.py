"""Role helpers and homeowner login lifecycle (invite, keep in step with Vantaca, deactivate)."""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import timezone

from .models import LoginLink, Owner, Profile

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


LINK_MINUTES = 15          # an emailed sign-in link works for this long, once
OWNER_SESSION_DAYS = 30    # an owner stays signed in on a device this long


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def link_user(token):
    """The unused, unexpired LoginLink for this token (and its user must still be active), else None."""
    ll = LoginLink.objects.select_related("user").filter(token_hash=_hash(token), used_at__isnull=True).first()
    if ll is None or not ll.user.is_active or ll.created_at < timezone.now() - timedelta(minutes=LINK_MINUTES):
        return None
    return ll


def send_signin_link(user, request=None):
    """Email the owner a one-time sign-in link (works with or without an HTTP request, e.g. from the Vantaca sync)."""
    token = secrets.token_urlsafe(32)
    LoginLink.objects.create(user=user, token_hash=_hash(token))
    path = reverse("signin_link", args=[token])
    url = request.build_absolute_uri(path) if request is not None else settings.SITE_URL + path
    name = user.first_name or "there"
    EmailMessage(f"Your sign-in link for {settings.HOA_NAME}",
                 f"Hello {name},\n\nClick this link to sign in to the {settings.HOA_NAME} owner portal:\n\n{url}\n\n"
                 f"The link works once and expires in {LINK_MINUTES} minutes. If you didn't ask for it, you can ignore this email.\n\n"
                 f"On behalf of the Board of Directors\n{settings.HOA_NAME}",
                 settings.DEFAULT_FROM_EMAIL, [user.email]).send()


def household_logins(owner):
    return owner.profiles.select_related("user").order_by("pk")


def can_add_login(owner):
    return household_logins(owner).filter(user__is_active=True).count() < settings.MAX_LOGINS_PER_HOUSEHOLD


def invite_member(owner, email, name="", relationship="owner", request=None):
    """Give one more person a login on this household (everyone on an owner record sees the same cases and messages).
    Emails them a sign-in link. Returns the Profile, or None if the email is blank/taken or the household is full."""
    email = (email or "").strip().lower()
    User = get_user_model()
    if not email or not owner.active or not can_add_login(owner):
        return None
    if User.objects.filter(username__iexact=email).exists():
        return None
    first, _, last = (name or "").strip().partition(" ")
    user = User.objects.create_user(email, email, secrets.token_urlsafe(24), first_name=first[:150], last_name=last[:150])
    profile = Profile.objects.create(user=user, role=Profile.Role.HOMEOWNER, owner=owner, relationship=relationship)
    send_signin_link(user, request)
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
    """Public sign-in page: if the email is on a household record, email them a one-time sign-in link (creating their login the
    first time). Returns True if the email is on file. Managers, board and committee don't use this: they sign in with a password."""
    owner = household_for_email(email)
    if not owner:
        return False
    User = get_user_model()
    user = User.objects.filter(username__iexact=email).first() or User.objects.filter(email__iexact=email).first()
    if user:
        if user.is_active and role_of(user) == "homeowner":
            send_signin_link(user, request)
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
