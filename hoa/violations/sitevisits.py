"""Site-visit observations: what the Compliance Committee sees on its drive-through before a meeting, kept as INTERNAL, add-only evidence.

Who may see or add them: committee members, the Board and the manager, and never an owner (not even a committee or Board member whose OWN
violation, or whose household's, it is). They are separate from the committee's formal findings and decisions and never become one."""
import hashlib

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction

from . import accounts, services, throttle, uploads
from .models import SiteVisitAccess, SiteVisitObservation, SiteVisitPhoto, Violation

ROLES = ("manager", "board", "committee")
MAX_COMMENT = 5000
A = SiteVisitAccess.Action
WHY = {"role": "Site-visit observations are only for the committee, the Board and the manager.",
       "draft": "This violation has not been issued yet.",
       "own": "This is your own (or your household's) violation, so you can't see or add site-visit observations on it.",
       "conflict": "This is your own (or your household's) violation, so you can't see or add site-visit observations on it.",
       "scope": "This case is not one the committee is working on yet."}


def blocked_reason(user, v):
    """None if `user` may see and add site-visit observations on case `v`, else a short code (see WHY)."""
    if accounts.role_of(user) not in ROLES:
        return "role"
    if v.status == Violation.Status.DRAFT:
        return "draft"
    o = accounts.owner_of(user)
    if o is not None and o.id == v.owner_id:
        return "own"
    if any(m.user_id == user.pk for m in services.conflicted_members(v)):
        return "conflict"
    if accounts.role_of(user) == "committee":
        from .views import can_view
        if not can_view(user, v):
            return "scope"
    return None


def allowed(user, v):
    return blocked_reason(user, v) is None


def _name(user):
    return ((user.get_full_name() or user.get_username()) if user is not None else "")[:200]


def log_access(user, v, action, request=None, observation=None, photo=None, detail=""):
    """Write one line of the audit history for the site-visit observations of a case. Add-only."""
    return SiteVisitAccess.objects.create(
        violation=v, user=user if user is not None and user.is_authenticated else None, user_name=_name(user), role=(accounts.role_of(user) or "") if user else "",
        action=action, observation=observation, photo=photo, detail=detail[:200], ip_address=(throttle.client_ip(request) if request is not None else "")[:45])


def log_view(user, v, request):
    """Record that `user` looked at the observations of `v`: once per person per case per 10 minutes, so a page refresh does not flood the log."""
    try:
        if not cache.add(f"sv-view:{user.pk}:{v.pk}", 1, 600):
            return
    except Exception:       # noqa: BLE001  (a broken counter store must not hide an access: log it)
        pass
    log_access(user, v, A.VIEWED, request)


def panel(user, v, request, back="meeting"):
    """What a page needs to show the observations of case `v` to `user`: {'allowed', 'items', 'count', 'back', 'why'}. Records the view."""
    why = blocked_reason(user, v)
    if why:
        return {"allowed": False, "items": [], "count": 0, "back": back, "why": WHY[why]}
    log_view(user, v, request)
    items = list(v.site_visits.select_related("corrects").prefetch_related("photos"))
    return {"allowed": True, "items": items, "count": len(items), "back": back, "why": ""}


def add(user, v, comment, files, corrects_id=None, request=None, rejected=None):
    """Save a new observation (written note and/or photographs). Returns it. Raises services.WorkflowError with a plain message.
    `rejected` (a list) collects (file name, reason) for pictures that were not kept. The original picture bytes are stored untouched."""
    why = blocked_reason(user, v)
    if why:
        raise services.WorkflowError(WHY[why])
    rejected = rejected if rejected is not None else []
    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT:
        raise services.WorkflowError(f"Keep the note under {MAX_COMMENT:,} characters (it is {len(comment):,}).")
    corrects = None
    if corrects_id not in (None, ""):
        corrects = v.site_visits.filter(pk=corrects_id if str(corrects_id).isdigit() else 0).first()
        if corrects is None:
            raise services.WorkflowError("That observation is not on this case.")
        if not comment:
            raise services.WorkflowError("Say what is being corrected or added.")
    keep, total = [], 0
    for f in files:
        if len(keep) >= uploads.MAX_PHOTOS_PER_UPLOAD:
            rejected.append((f.name, f"only {uploads.MAX_PHOTOS_PER_UPLOAD} photos can be added at a time"))
        elif f.size > uploads.MAX_UPLOAD:
            rejected.append((f.name, f"larger than {uploads.MAX_UPLOAD // (1024 * 1024)} MB"))
        elif total + f.size > uploads.MAX_UPLOAD_TOTAL:
            rejected.append((f.name, "too much to send at once; add the rest separately"))
        elif not (ext := uploads.image_ext(f)):
            rejected.append((f.name, "not a JPG, PNG, GIF or WebP picture"))
        else:
            taken = uploads.taken_at(f)
            data = f.read()
            f.seek(0)
            keep.append((f, ext, data, taken))
            total += f.size
    if not comment and not keep:
        raise services.WorkflowError("Write a note or add a photo.")
    with transaction.atomic():
        obs = SiteVisitObservation.objects.create(
            violation=v, meeting=v.committee_meeting, author=user, author_name=_name(user), author_role=accounts.role_of(user) or "",
            comment=comment, corrects=corrects)
        for f, ext, data, taken in keep:
            p = SiteVisitPhoto(observation=obs, sha256=hashlib.sha256(data).hexdigest(), size=len(data), original_name=(f.name or "")[:200], taken_at=taken)
            p.file.save(f"photo.{ext}", ContentFile(data), save=False)
            p.save()
        services.log(v, f"Site-visit observation #{obs.pk} " + (f"added as a correction to #{corrects.pk}" if corrects else "added")
                     + f" by {obs.author_name}" + (f" ({len(keep)} photo{'s' if len(keep) != 1 else ''})" if keep else ""), user)
        log_access(user, v, A.CORRECTED if corrects else A.ADDED, request, observation=obs, detail=f"{len(keep)} photo(s)")
    return obs
