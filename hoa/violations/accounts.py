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
    """The owner record this login belongs to. A committee, board or manager login can ALSO be an owner's login: the same person
    signs in once, lands on their own violations, and reaches the extra role from there."""
    p = getattr(user, "profile", None)
    return p.owner if p and p.owner_id else None


class AccountError(Exception):
    """A refused account action (shown to the person who tried it)."""


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


def revoke_links(user):
    """Cancel every sign-in link this person has not used yet (a new link was sent, or their email address changed). Returns how many."""
    return LoginLink.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())


def purge_old_links(days=2):
    """Delete sign-in links that are long dead (they only work for 15 minutes). Returns how many."""
    return LoginLink.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()[0]


def send_signin_link(user, request=None):
    """Email the owner a one-time sign-in link (works with or without an HTTP request, e.g. from the Vantaca sync).
    Only the newest link works: asking again cancels any earlier one."""
    revoke_links(user)
    token = secrets.token_urlsafe(32)
    LoginLink.objects.create(user=user, token_hash=_hash(token))
    path = reverse("signin_link", args=[token])
    # On the live site the address in the email always comes from SITE_URL, never from the address the request arrived on.
    url = request.build_absolute_uri(path) if (request is not None and not settings.PRODUCTION) else settings.SITE_URL + path
    name = user.first_name or "there"
    EmailMessage(f"Your {settings.APP_NAME} sign-in link",
                 f"Hello {name},\n\nClick this link to sign in to the {settings.APP_NAME}:\n\n{url}\n\n"
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
        if user.is_active and owner_of(user) is not None:      # owners, including owners who are also on the committee or Board
            send_signin_link(user, request)
        return True
    name = owner.tenant_name if _relationship_for(owner, email) == "tenant" else owner.display_name
    return invite_member(owner, email, name, _relationship_for(owner, email), request) is not None


def _still_listed(owner, email, relationship):
    """Is this household member still on the owner record? Tenants only while the lease is active."""
    e = (email or "").lower()
    if relationship == "tenant":
        return owner.has_tenant and e in {x.lower() for x in owner.tenant_emails}
    return e in {x.lower() for x in owner.all_emails}


def ensure_login(owner, email="", name="", relationship="owner"):
    """The owner's login, created quietly (no email sent) if they don't have one yet. Used when an owner is picked for the committee."""
    User = get_user_model()
    email = (email or owner.email or "").strip().lower()
    existing = owner.profiles.filter(user__username__iexact=email).select_related("user").first() or \
        owner.profiles.filter(relationship=relationship).select_related("user").first() or owner.profiles.select_related("user").first()
    if existing:
        return existing.user
    if not email or User.objects.filter(username__iexact=email).exists():
        return User.objects.filter(username__iexact=email).first() if email else None
    first, _, last = (name or owner.display_name).partition(" ")
    user = User.objects.create_user(email, email, secrets.token_urlsafe(24), first_name=first[:150], last_name=last[:150])
    Profile.objects.create(user=user, role=Profile.Role.HOMEOWNER, owner=owner, relationship=relationship)
    return user


def sync_committee_logins(year=None):
    """Committee logins follow the roster: a member's login is on while they are an active member for the current year and off
    otherwise (members change each year). Only logins linked to a roster entry are touched. Returns (switched_on, switched_off)."""
    from .models import CommitteeMember
    year = year or timezone.localdate().year
    on = off = 0
    linked = set(CommitteeMember.objects.exclude(user=None).values_list("user_id", flat=True))
    current = set(CommitteeMember.objects.filter(year=year, active=True).exclude(user=None).values_list("user_id", flat=True))
    for u in get_user_model().objects.filter(pk__in=linked):
        p = getattr(u, "profile", None)
        if p is None or p.role in (Profile.Role.MANAGER, Profile.Role.BOARD):
            continue
        want = u.pk in current
        if p.owner_id:                       # an owner who serves on the committee: one login, the committee role comes and goes with the roster
            new = Profile.Role.COMMITTEE if want else Profile.Role.HOMEOWNER
            if p.role != new:
                p.role = new
                p.save()
                on += int(want)
                off += int(not want)
        elif p.role == Profile.Role.COMMITTEE and u.is_active != want:
            u.is_active = want
            u.save(update_fields=["is_active"])
            on += int(want)
            off += int(not want)
    return on, off


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
        elif user.is_active and profile.relationship != "owner" and not _still_listed(owner, user.email, profile.relationship):
            user.is_active = False                    # a tenant whose lease ended or who was removed, or a co-owner no longer listed
            user.save(update_fields=["is_active"])
            result = "deactivated"
    if owner.active:
        owner_profile = next((p for p in profiles if p.relationship == "owner"), None)
        new = owner.email
        User = get_user_model()
        if owner_profile and new and owner_profile.user.email.lower() != new.lower() \
                and not User.objects.filter(username__iexact=new).exclude(pk=owner_profile.user_id).exists():
            u = owner_profile.user
            old = u.email
            u.username = u.email = new  # the owner's login follows the Vantaca email; the old address stops working
            u.save()
            result = "updated"
            from . import audit            # the access change is audited, unused links are cancelled, sessions end, the old address is told
            signed_out = end_sessions(u)
            audit.record("signin_email.changed", f"{owner.property_address} (owner, {u.get_full_name() or old})",
                         f"{old} -> {new} (changed by the Vantaca sync). Unused links cancelled: {revoke_links(u)}; sessions ended: {signed_out}.")
            try:
                _notify_old_address(owner.property_address, old, new, "")
            except Exception as e:      # noqa: BLE001
                audit.record("signin_email.mail_failed", owner.property_address, f"notice: {type(e).__name__}")
    return result


# ---------- controlled account actions (SEC-05, SEC-14, SEC-17); each one is written to the audit log ----------
def end_sessions(user):
    """Sign this person out everywhere: delete every browser session that belongs to them. Returns how many."""
    from django.contrib.sessions.models import Session
    n = 0
    for s in Session.objects.filter(expire_date__gt=timezone.now()):
        if s.get_decoded().get("_auth_user_id") == str(user.pk):
            s.delete()
            n += 1
    return n


def _mask(email):
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "(no address)"


def _notify_old_address(owner_label, old, new, by):
    """Tell the OLD address its sign-in email was changed (so a wrong or hostile change is noticed). Never shows the full new address."""
    if not old:
        return
    EmailMessage(f"The sign-in email for your {settings.APP_NAME} account was changed",
                 f"Hello,\n\nThe sign-in email for {owner_label} on the {settings.APP_NAME} was changed from this address to {_mask(new)} "
                 f"on {timezone.localdate():%b} {timezone.localdate().day}, {timezone.localdate().year}{(' by ' + by) if by else ''}.\n\n"
                 f"If you did not expect this, please contact the Board right away. This address can no longer be used to sign in.\n\n"
                 f"On behalf of the Board of Directors\n{settings.HOA_NAME}",
                 settings.DEFAULT_FROM_EMAIL, [old]).send()


def change_signin_email(profile, new_email, actor, reason, request=None):
    """Staff action: change one household login's sign-in email. In one step it updates the login AND the owner record's matching address,
    cancels unused sign-in links, signs the person out everywhere, and writes the audit entry; then it emails the OLD address a notice and
    sends the first sign-in link to the NEW address (opening it is the proof the address is right). NOTE: Vantaca stays the source of
    truth, so the next sync restores Vantaca's address unless Vantaca is updated too. Returns a dict describing what happened."""
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from . import audit
    if role_of(actor) not in STAFF_ROLES:
        raise AccountError("Only the Board or manager can change a sign-in email.")
    reason = (reason or "").strip()
    if not reason:
        raise AccountError("Say why the address is being changed (it is kept in the audit log).")
    new = (new_email or "").strip().lower()
    try:
        validate_email(new)
    except ValidationError:
        raise AccountError("That doesn't look like a valid email address.")
    if len(new) > 150:
        raise AccountError("That address is too long.")
    user, owner = profile.user, profile.owner
    if owner is None:
        raise AccountError("This login is not part of an owner household.")
    old = user.email
    if new == (old or "").lower():
        raise AccountError("That is already this login's sign-in email.")
    User = get_user_model()
    if User.objects.filter(username__iexact=new).exclude(pk=user.pk).exists() or User.objects.filter(email__iexact=new).exclude(pk=user.pk).exists():
        raise AccountError("Another login already uses that address.")
    other = household_for_email(new)
    if other is not None and other.pk != owner.pk:
        raise AccountError("That address is on a different household's record. It can't be used here.")
    field = {"owner": "email", "co_owner": "co_owner_email", "tenant": "tenant_email"}.get(profile.relationship)
    with transaction.atomic():
        user.username = user.email = new
        user.save(update_fields=["username", "email"])
        if field:
            setattr(owner, field, new)
            owner.save(update_fields=[field])
        revoked, signed_out = revoke_links(user), end_sessions(user)
        audit.record("signin_email.changed", f"{owner.property_address} ({profile.relationship}, {user.get_full_name() or old})",
                     f"{old} -> {new}. Reason: {reason}. Unused links cancelled: {revoked}; sessions ended: {signed_out}. "
                     f"Owner record address updated: {'yes (' + field + ')' if field else 'no'}.", actor=actor, request=request)
    out = {"old": old, "new": new, "notice_sent": False, "link_sent": False}
    for key, fn in (("notice_sent", lambda: _notify_old_address(owner.property_address, old, new, actor.get_full_name() or actor.get_username())),
                    ("link_sent", lambda: send_signin_link(user, request))):
        try:
            fn()
            out[key] = True
        except Exception as e:      # noqa: BLE001  (a mail problem must not undo the change; it is recorded)
            audit.record("signin_email.mail_failed", owner.property_address, f"{key}: {type(e).__name__}", actor=actor, request=request)
    return out


def reset_two_step(user, actor, reason, request=None):
    """Staff action for a lost phone: remove this person's authenticator so they can enrol a new one at their next sign-in. A DIFFERENT
    Board member/manager must do it (never your own), a reason is required, the person is signed out everywhere and emailed, and it is audited."""
    from django_otp import devices_for_user
    from . import audit
    if role_of(actor) not in STAFF_ROLES:
        raise AccountError("Only the Board or manager can reset two-step sign-in.")
    if actor.pk == user.pk:
        raise AccountError("You can't reset your own two-step sign-in: another Board member or the manager must do it after confirming who you are.")
    reason = (reason or "").strip()
    if not reason:
        raise AccountError("Say how the person's identity was confirmed and why (it is kept in the audit log).")
    devices = list(devices_for_user(user, confirmed=None))
    if not devices:
        raise AccountError("This person has no authenticator set up, so there is nothing to reset.")
    for d in devices:
        d.delete()
    signed_out = end_sessions(user)
    audit.record("two_step.reset", user.get_full_name() or user.get_username(),
                 f"{len(devices)} authenticator(s) removed; sessions ended: {signed_out}. Reason: {reason}", actor=actor, request=request)
    mailed = False
    if user.email:
        try:
            EmailMessage(f"Your {settings.APP_NAME} two-step sign-in was reset",
                         f"Hello {user.first_name or 'there'},\n\nYour two-step sign-in (authenticator app) for the {settings.APP_NAME} was reset by "
                         f"{actor.get_full_name() or actor.get_username()}. You will be asked to set it up again the next time you sign in.\n\n"
                         f"If you did not ask for this, tell the Board right away.\n\nOn behalf of the Board of Directors\n{settings.HOA_NAME}",
                         settings.DEFAULT_FROM_EMAIL, [user.email]).send()
            mailed = True
        except Exception as e:      # noqa: BLE001
            audit.record("two_step.mail_failed", user.get_username(), type(e).__name__, actor=actor, request=request)
    return {"devices": len(devices), "signed_out": signed_out, "emailed": mailed}
