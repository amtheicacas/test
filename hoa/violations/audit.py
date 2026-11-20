"""The account and record audit trail (`AuditLog`). One function writes entries; nothing reads secrets into them.

Never pass a password, token, key or file contents as `detail`. Old -> new values of ordinary fields are fine."""
from .models import AuditLog

SECRET_FIELDS = ("password", "token", "secret", "key")


def _actor(user):
    if user is None or not getattr(user, "pk", None):
        return None, "system", ""
    p = getattr(user, "profile", None)
    return user, (user.get_full_name() or user.get_username())[:200], (p.role if p else ("manager" if user.is_superuser else ""))


def record(action, subject="", detail="", actor=None, request=None):
    """Write one audit entry. `actor` may be None for the sync and scheduled jobs; `request` adds the visitor's address."""
    if actor is None and request is not None and getattr(request, "user", None) is not None and request.user.is_authenticated:
        actor = request.user
    user, name, role = _actor(actor)
    ip = None
    if request is not None:
        from .throttle import client_ip
        ip = client_ip(request) or None
    return AuditLog.objects.create(actor=user, actor_name=name, actor_role=role, action=action[:60], subject=str(subject)[:300],
                                   detail=str(detail)[:5000], ip_address=ip)


def diff(old, new, fields):
    """'email: a@x.com -> b@x.com; phone: 1 -> 2' for the fields that differ (values shortened; secret-looking field names are only named)."""
    out = []
    for f in fields:
        a, b = old.get(f, ""), new.get(f, "")
        if a != b:
            if any(s in f.lower() for s in SECRET_FIELDS):
                out.append(f"{f} changed")
            else:
                out.append(f"{f}: {str(a)[:80] or '(blank)'} -> {str(b)[:80] or '(blank)'}")
    return "; ".join(out)
