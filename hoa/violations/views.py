import hashlib
from datetime import date, timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import accounts, audit, casefile, minutesfiles, forms, geocoding, ledger, services, sitevisits, standards_search, throttle, uploads, vantaca
from .models import AuditLog, SentLetter, SiteVisitPhoto, Profile, short_date, CannedReply, CommitteeMember, MailPiece, MeetingAttendance, CommitteeMeeting, Notice, Owner, Photo, Response, Violation
from .pdf import render_notice_pdf

S = Violation.Status
MAX_UPLOAD = uploads.MAX_UPLOAD
MAX_IMPORT_BYTES = 10 * 1024 * 1024                         # the owner-list upload (a real list is a few hundred KB)
MAX_PHOTOS_PER_UPLOAD = uploads.MAX_PHOTOS_PER_UPLOAD       # photos accepted in one submission
MAX_UPLOAD_TOTAL = uploads.MAX_UPLOAD_TOTAL                 # ...and their combined size
MAX_RESIDENT_PHOTOS_PER_CASE = 30                 # photos an owner (or anyone holding the private link) can add to one case
RESIDENT_POSTS_PER_HOUR = 10                      # messages from one private link per hour
STAFF = ("manager", "board")
COMMITTEE_VISIBLE = (S.HEARING_SCHEDULED, S.LEDGER_PENDING, S.FINE_APPROVED, S.FINE_REJECTED, S.CLOSED, S.REFERRED_LEGAL, S.WAIVED)


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(request, *a, **kw):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if accounts.role_of(request.user) not in roles:
                raise PermissionDenied
            return fn(request, *a, **kw)
        return wrapper
    return deco


def _safe_next(request):
    """The page to return to after a form (the `next` field), but ONLY an address on this site: anything else (another website, a script
    address, a bare word) is ignored, so a crafted form can't send a Board member somewhere else after they act."""
    return accounts.local_path(request.POST.get("next"), request.get_host(), request.is_secure())


def _int(value, default=0):
    """A number typed or posted by a person: anything that is not a whole number becomes `default` (never a server error)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _date(value):
    """A date typed or put in a link by a person: anything that is not a real calendar date (YYYY-MM-DD) becomes None (never a server error)."""
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


staff_only = role_required(*STAFF)
staff_or_committee = role_required(*STAFF, "committee")
def owner_only(fn):
    """Pages about the signed-in person's OWN violations. Anyone whose login is linked to an owner record can use them, including a
    committee or Board member (one login for everything)."""
    @wraps(fn)
    def wrapper(request, *a, **kw):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if accounts.owner_of(request.user) is None:
            raise PermissionDenied
        return fn(request, *a, **kw)
    return wrapper


homeowner_only = owner_only


def owns(user, v):
    o = accounts.owner_of(user)
    return bool(o) and v.owner_id == o.id and v.status != S.DRAFT


def can_view(user, v):
    """Per-case access: staff see all; committee only cases sent to a meeting; homeowners only their own."""
    role = accounts.role_of(user)
    if owns(user, v):                       # your own violation: always visible to you, whatever other role you hold
        return True
    if role in STAFF:
        return True
    if role == "committee":
        # the committee sees a case once the Board has voted on it (waiting for a meeting), and from then on through its meeting
        return v.status == S.HEARING_NEEDED or (v.committee_meeting_id is not None and v.status in COMMITTEE_VISIBLE)
    if role == "homeowner":
        o = accounts.owner_of(user)
        return bool(o) and v.owner_id == o.id and v.status != S.DRAFT
    return False


def _taken_at(f):
    """When the photo was taken, from EXIF (DateTimeOriginal), or None."""
    return uploads.taken_at(f)


def _save_photos(v, files, from_resident=False, rejected=None):
    """Save the uploaded pictures; returns how many were kept. The stored type is the picture's real type (never the typed file name).
    Anything not kept is added to `rejected` as (file name, reason) so the person is told."""
    rejected = rejected if rejected is not None else []
    n = total = 0
    room = MAX_RESIDENT_PHOTOS_PER_CASE - v.photos.filter(from_resident=True).count() if from_resident else None
    for f in files:
        if n >= MAX_PHOTOS_PER_UPLOAD:
            rejected.append((f.name, f"only {MAX_PHOTOS_PER_UPLOAD} photos can be added at a time"))
        elif room is not None and n >= room:
            rejected.append((f.name, "this case already has the most photos allowed"))
        elif f.size > MAX_UPLOAD:
            rejected.append((f.name, f"larger than {MAX_UPLOAD // (1024 * 1024)} MB"))
        elif total + f.size > MAX_UPLOAD_TOTAL:
            rejected.append((f.name, "too much to send at once; add the rest separately"))
        elif not (ext := uploads.image_ext(f)):
            rejected.append((f.name, "not a JPG, PNG, GIF or WebP picture"))
        else:
            p = Photo(violation=v, from_resident=from_resident, taken_at=_taken_at(f))
            p.image.save(f"photo.{ext}", f, save=False)
            p.save()
            n += 1
            total += f.size
    return n


def _report_rejected(request, rejected):
    if rejected:
        shown = "; ".join(f"{name} ({why})" for name, why in rejected[:4])
        more = f"; and {len(rejected) - 4} more" if len(rejected) > 4 else ""
        messages.warning(request, f"{len(rejected)} file(s) were not added: {shown}{more}.")


def _run(request, fn, *a, ok=None, **kw):
    try:
        res = fn(*a, **kw)
        if ok:
            messages.success(request, ok)
        return res
    except services.WorkflowError as e:
        messages.error(request, str(e))


def _awaiting_reply():
    """Owner messages with no staff reply since; the procedure promises a response within 48-72 hours."""
    later_staff = Response.objects.filter(violation=OuterRef("violation"), from_resident=False,
                                          created_at__gt=OuterRef("created_at"))
    qs = (Response.objects.filter(from_resident=True, violation__status__in=Violation.OPEN)
          .annotate(answered=Exists(later_staff)).filter(answered=False)
          .select_related("violation__owner").order_by("created_at"))
    cutoff = timezone.now() - timedelta(hours=settings.REPLY_TARGET_HOURS)
    return [(r, r.created_at < cutoff) for r in qs]


def _verify_list():
    """Cases marked for re-inspection, oldest first: to be verified before the next Board meeting."""
    return list(Violation.objects.filter(reinspect_on__isnull=False, status__in=Violation.OPEN).exclude(status=S.DRAFT)
                .select_related("owner", "vtype").order_by("reinspect_on"))


# ---------- landing / dashboard ----------
def home(request):
    """Where "/" leads: owners (even when also on the committee or Board) start on their own violations; others go to their workspace."""
    if not request.user.is_authenticated:
        return redirect_to_login("/")
    role = accounts.role_of(request.user)
    if accounts.owner_of(request.user) is not None:
        return redirect("my_home")
    if role == "committee":
        return redirect("committee_home")
    if role not in STAFF:
        raise PermissionDenied
    return _dashboard(request)


@staff_only
def workspace(request):
    """The Board / Manager dashboard (also reachable from an owner's home when they hold a Board or manager role)."""
    return _dashboard(request)


def _dashboard(request):
    services.advance_expired()
    counts = dict(Violation.objects.values_list("status").annotate(c=Count("id")))
    ctx = {
        "counts": [(label, counts.get(val, 0), val) for val, label in S.choices],
        "pending_requests": Response.objects.filter(extension_requested=True, handled=False,
                                                    violation__status__in=Violation.OPEN).select_related("violation__owner"),
        "expiring": Violation.objects.filter(status=S.NOTICE_SENT).select_related("owner", "vtype").order_by("cure_deadline")[:8],
        "board_queue": Violation.objects.filter(status=S.PENDING_BOARD).count(),
        "need_hearing": Violation.objects.filter(status=S.HEARING_NEEDED).count(),
        "unmailed": Notice.objects.filter(mailed_on__isnull=True, mail_cancelled=False).filter(
            Q(mail_hold_until__isnull=True) | Q(mail_hold_until__lte=date.today())).count(),
        "held_mail": Notice.objects.filter(mailed_on__isnull=True, mail_cancelled=False, mail_hold_until__gt=date.today()).count(),
        "reinspect": Response.objects.filter(cure_reported=True, handled=False,
                                             violation__status__in=Violation.OPEN).select_related("violation__owner"),
        "verify_list": _verify_list(),
        "results_waiting": CommitteeMeeting.objects.filter(results_status="submitted").annotate(n=Count("violations", filter=Q(violations__status=S.LEDGER_PENDING))),
        "mail_problems": MailPiece.objects.filter(service="certified", problem=True, delivered_on__isnull=True, handled_on__isnull=True)
                         .select_related("notice__violation__owner").order_by("problem_on", "pk"),
        "certified_to_check": services.certified_to_check(),
        "pieces_to_mail": MailPiece.objects.filter(by_hand=True, mailed_on__isnull=True, handled_on__isnull=True)
                          .select_related("notice__violation__owner").order_by("notice__created_at", "pk"),
        "legal_drafts": Violation.objects.filter(status=S.REFERRED_LEGAL, legal_sent_on__isnull=True).select_related("owner", "vtype"),
        "awaiting_reply": _awaiting_reply(),
        "unpaid": Violation.objects.filter(status=S.FINE_APPROVED).select_related("owner", "vtype").order_by("fine_due_on"),
        "ledger_pending": Violation.objects.filter(status=S.LEDGER_PENDING).select_related("owner", "vtype"),
        "today": date.today(),
    }
    ctx.update(_dashboard_visuals(counts, request.GET.get("range", "12m")))
    return render(request, "violations/dashboard.html", ctx)


OVERDUE = [  # key, label, what makes it overdue
    ("board", "Waiting on a Board vote too long", "the cure period ended more than BOARD_REVIEW_TARGET_DAYS ago and the Board has not voted"),
    ("hearing", "Hearing date passed, no decision", "the Compliance Committee meeting has happened but no decision is recorded"),
    ("fine", "Fine past its payment due date", "imposed fine not paid by the due date"),
    ("letter3", "Decision letter not sent in time", "the Committee decided but the written findings letter has not gone out within DECISION_LETTER_DAYS of the hearing"),
]


def overdue_qs(key, today=None):
    today = today or date.today()
    if key == "board":
        return Violation.objects.filter(status=S.PENDING_BOARD,
                                        cure_deadline__lt=today - timedelta(days=settings.BOARD_REVIEW_TARGET_DAYS))
    if key == "hearing":
        return Violation.objects.filter(status=S.HEARING_SCHEDULED, hearing_date__lt=timezone.now())
    if key == "letter3":
        return Violation.objects.filter(status=S.LEDGER_PENDING, hearing_date__lt=timezone.now() - timedelta(days=settings.DECISION_LETTER_DAYS))
    if key == "fine":
        return Violation.objects.filter(status=S.FINE_APPROVED, fine_amount__gt=0, fine_paid_on__isnull=True, fine_due_on__lt=today)
    return Violation.objects.none()


PIPELINE = [("Re-inspect (verify)", "reinspect"), ("Cure period", "notice_sent"), ("Board vote", "pending_board"), ("Schedule hearing", "hearing_needed"),
            ("Committee / hearing", "hearing_scheduled"), ("Post to ledger", "ledger_pending"),
            ("Imposed", "fine_approved"), ("On hold", "on_hold")]


RANGES = [("30d", "30 days", 30), ("12m", "12 months", 365), ("all", "All time", None)]


def _decision_speed(days, today):
    """How long cases take from the notice being issued to the Compliance Committee's decision (the hearing date), over the chosen window:
    summary numbers, where the time goes (cure period, waiting for the Board, waiting for the hearing) and a monthly average."""
    import statistics
    qs = (Violation.objects.exclude(status=S.DRAFT).exclude(committee_outcome="").filter(hearing_date__isnull=False)
          .prefetch_related("notices"))
    rows, phases, monthly = [], {"cure": [], "board": [], "hearing": []}, {}
    for v in qs:
        decided = timezone.localtime(v.hearing_date).date()
        if days and decided < today - timedelta(days=days):
            continue
        first = next((n for n in v.notices.all() if n.kind == "violation"), None)
        opened = timezone.localtime(first.created_at).date() if first else timezone.localtime(v.created_at).date()
        total = max((decided - opened).days, 0)
        rows.append(total)
        monthly.setdefault((decided.year, decided.month), []).append(total)
        cure_end = v.cure_deadline if v.cure_deadline and opened <= v.cure_deadline <= decided else None
        if cure_end:
            phases["cure"].append((cure_end - opened).days)
        if v.board_meeting_date and decided >= v.board_meeting_date >= (cure_end or opened):
            phases["board"].append((v.board_meeting_date - (cure_end or opened)).days)
            phases["hearing"].append((decided - v.board_meeting_date).days)
    if not rows:
        return {"n": 0}
    avg = lambda xs: round(sum(xs) / len(xs)) if xs else 0
    parts = [("Cure period", avg(phases["cure"])), ("Waiting for the Board vote", avg(phases["board"])), ("Waiting for the hearing", avg(phases["hearing"]))]
    sum_parts = sum(d for _, d in parts) or 1
    colors = ["#2563c9", "#d9822b", "#7a5cc7"]
    segs = [{"label": l, "days": d, "pct": round(100 * d / sum_parts), "color": colors[i]} for i, (l, d) in enumerate(parts)]
    months = sorted(monthly)[-12:]
    peak = max((avg(monthly[k]) for k in months), default=1) or 1
    bars = [{"label": date(y, m, 1).strftime("%b %y" if len(months) > 6 else "%b"), "avg": avg(monthly[(y, m)]), "n": len(monthly[(y, m)]),
             "pct": round(100 * avg(monthly[(y, m)]) / peak)} for y, m in months]
    return {"n": len(rows), "median": round(statistics.median(rows)), "avg": avg(rows), "fastest": min(rows), "slowest": max(rows),
            "segs": segs, "bars": bars}


def _dashboard_visuals(counts, rng="12m"):
    """Numbers behind the dashboard graphics: pipeline, top rule types, 6-month trend, headline tiles."""
    today = date.today()
    counts = {**counts, "reinspect": Violation.objects.filter(reinspect_on__isnull=False, status__in=Violation.OPEN).count()}
    peak = max([counts.get(k, 0) for _, k in PIPELINE] + [1])
    pipeline = [{"label": l, "key": k, "n": counts.get(k, 0), "pct": round(100 * counts.get(k, 0) / peak)}
                for l, k in PIPELINE]
    days = {k: d for k, _, d in RANGES}.get(rng, 365)
    rng = rng if rng in {k for k, _, _ in RANGES} else "12m"
    top_qs = Violation.objects.exclude(status=S.DRAFT)
    if days:   # after years of data an all-time chart stops being useful, so it defaults to the last 12 months
        top_qs = top_qs.filter(observed_on__gte=today - timedelta(days=days))
    top = list(top_qs.values("vtype__name").annotate(n=Count("id")).order_by("-n", "vtype__name")[:6])
    top_peak = max([t["n"] for t in top] + [1])
    for t in top:
        t["pct"] = round(100 * t["n"] / top_peak)
    speed = _decision_speed(days, today)
    open_qs = Violation.objects.filter(status__in=Violation.OPEN)
    cured_30 = Violation.objects.filter(cured_on__gte=today - timedelta(days=30)).count()
    total = Violation.objects.count()
    cured_all = sum(counts.get(k, 0) for k in ("cured", "closed"))
    overdue = [{"key": k, "label": l, "hint": h.replace("BOARD_REVIEW_TARGET_DAYS", f"{settings.BOARD_REVIEW_TARGET_DAYS} days").replace("DECISION_LETTER_DAYS", f"{settings.DECISION_LETTER_DAYS} days"), "n": overdue_qs(k, today).count()} for k, l, h in OVERDUE]
    late_replies = sum(1 for _, late in _awaiting_reply() if late)
    overdue.append({"key": "replies", "label": "Owner messages unanswered past the reply target",
                    "hint": f"no staff reply within {settings.REPLY_TARGET_HOURS} hours", "n": late_replies})
    return {
        "overdue": overdue, "overdue_total": sum(o["n"] for o in overdue),
        "pipeline": pipeline, "top_types": top, "speed": speed, "ranges": [(k, l) for k, l, _ in RANGES], "range": rng,
        "kpi_open": open_qs.count(),
        "kpi_homes": open_qs.values("owner").distinct().count(),
        "kpi_cured30": cured_30,
        "kpi_rate": round(100 * cured_all / total) if total else None,
        "kpi_fines": Violation.objects.filter(status__in=["fine_approved", "closed"], fine_amount__isnull=False)
                     .aggregate(t=Sum("fine_amount"))["t"] or 0,
    }


# ---------- staff / board ----------
@staff_only
def violation_list(request):
    qs = Violation.objects.select_related("owner", "vtype").annotate(
        unread=Exists(Response.objects.filter(violation=OuterRef("pk"), from_resident=True, read_at__isnull=True)))
    g = request.GET
    status, q, vtype = g.get("status", ""), g.get("q", "").strip(), g.get("type", "")
    if status == "open":
        qs = qs.filter(status__in=Violation.OPEN)
    elif status:
        qs = qs.filter(status=status)
    type_id = _int(vtype, 0)
    if 0 < type_id < 2 ** 31:               # a link edited by hand to something that is not a type number is simply not applied
        qs = qs.filter(vtype_id=type_id)
    if g.get("reinspect"):
        qs = qs.filter(reinspect_on__isnull=False, status__in=Violation.OPEN)
    if g.get("overdue") in ("board", "hearing", "fine", "letter3"):
        qs = qs.filter(pk__in=overdue_qs(g["overdue"]).values("pk"))
    if q:
        qs = qs.filter(Q(owner__name__icontains=q) | Q(owner__property_address__icontains=q)
                       | Q(description__icontains=q) | Q(vtype__name__icontains=q))
    frm, to = _date(g.get("from")), _date(g.get("to"))
    if frm:
        qs = qs.filter(observed_on__gte=frm)
    if to:
        qs = qs.filter(observed_on__lte=to)
    from .models import ViolationType
    return render(request, "violations/list.html", {"violations": qs, "status": status, "q": q, "statuses": S.choices,
                                                    "types": ViolationType.objects.filter(violation__isnull=False).distinct().order_by("name"), "vtype": vtype,
                                                    "frm": frm.isoformat() if frm else "", "to": to.isoformat() if to else ""})


@staff_only
def standards_search_api(request):
    """JSON for the violation picker. ?q=text (local search); add &ai=1 to also ask the AI."""
    q = request.GET.get("q", "").strip()[:500]
    local = [standards_search.serialize(t) for t in standards_search.local_search(q)]
    out = {"results": local, "ai": False, "ai_available": standards_search.ai_available()}
    if request.GET.get("ai") == "1" and q:
        if not throttle.hit(f"ai:{timezone.localdate()}", settings.AI_DAILY_LIMIT, 86400):
            ai = standards_search.ai_search(q)
        else:
            ai = None
            out["ai_note"] = f"The team's daily limit for AI search ({settings.AI_DAILY_LIMIT}) has been reached; showing keyword matches."
        if ai is None:
            out.setdefault("ai_note", "AI search isn't available right now; showing keyword matches.")
        else:
            out["ai"] = True
            seen = set()
            ai_items = []
            for vt, why in ai:
                d = standards_search.serialize(vt)
                d["reason"] = why
                ai_items.append(d)
                seen.add(vt.pk)
            out["results"] = ai_items + [r for r in local if r["id"] not in seen]
    return JsonResponse(out)


@staff_only
def violation_create(request):
    form = forms.ViolationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        v = form.save(commit=False)
        v.created_by = request.user
        timing = form.cleaned_data["timing"]
        if timing != "draft":
            v.issuance_timing = timing
        v.save()
        bad = []
        n = _save_photos(v, request.FILES.getlist("photos"), rejected=bad)
        _report_rejected(request, bad)
        services.log(v, f"Violation recorded with {n} photo(s)", request.user)
        if timing != "draft":
            held = timing == "standard"
            if _run(request, services.issue_first_notice, v, request.user, timing=timing,
                    ok=("Notice emailed. The mailed copy is held until %d business days from now." % settings.MAIL_DELAY_BUSINESS_DAYS)
                       if held else "Notice issued: emailed and sent to the print/mail provider.") is None:
                messages.info(request, "Saved as a draft instead.")
        return redirect(v)
    from .models import ViolationType
    catalog = [standards_search.serialize(t) for t in ViolationType.objects.exclude(code__isnull=True).order_by("section", "code")]
    owner_info = {str(o.pk): recipient_summary(o) for o in Owner.objects.filter(active=True)}
    return render(request, "violations/form.html", {"form": form, "catalog": catalog, "owner_info": owner_info,
                                                    "ai_available": standards_search.ai_available(),
                                                    "categories": sorted({c["category"] for c in catalog})})


def recipient_summary(o):
    """Who a notice for this home reaches (shown on the new-violation form): owner and, while the lease is active, the tenant."""
    def lines(kind):
        return o.address_block(kind).split("\n", 1)[1].replace("\n", ", ")
    owner = {"name": o.display_name, "emails": list(o.all_emails), "mail": [lines(k) for k in o.owner_mail_kinds()]}
    out = {"owner": owner, "address": o.property_address, "tenant": None, "tenant_note": ""}
    if o.has_tenant:
        out["tenant"] = {"name": o.tenant_name, "emails": list(o.tenant_emails) if o.tenant_is_emailed else [],
                         "mail": [lines("tenant")] if o.tenant_is_mailed else [], "skip_mail": not o.tenant_is_mailed,
                         "skip_email": not o.tenant_is_emailed,
                         "lease": " to ".join(short_date(d) for d in (o.tenant_start, o.tenant_end) if d)}
    elif o.tenant_name.strip():
        out["tenant_note"] = (f"A tenant ({o.tenant_name}) is on record but the lease "
                              + ("ended " + short_date(o.tenant_end) if o.tenant_lease_ended else "has not started")
                              + ", so they will not be notified.")
    return out


@staff_only
def canned_replies(request):
    """Standard responses the board and manager share. Anyone on the team can add, edit or delete one."""
    if request.method == "POST":
        P = request.POST
        act, title, body = P.get("action"), (P.get("title") or "").strip()[:80], (P.get("body") or "").strip()
        if act == "delete":
            gone = CannedReply.objects.filter(pk=_int(P.get("id"))).first()
            if gone:
                audit.record("standard_response.deleted", gone.title, "", request=request)
            CannedReply.objects.filter(pk=_int(P.get("id"))).delete()
            messages.success(request, "Deleted.")
        elif not title or not body:
            messages.error(request, "A standard response needs a title and the text.")
        elif act == "update":
            CannedReply.objects.filter(pk=_int(P.get("id"))).update(title=title, body=body)
            audit.record("standard_response.changed", title, "", request=request)
            messages.success(request, "Saved.")
        else:
            CannedReply.objects.create(title=title, body=body, created_by=request.user)
            audit.record("standard_response.added", title, "", request=request)
            messages.success(request, "Canned reply added. Everyone on the team can use it.")
        return redirect("canned_replies")
    return render(request, "violations/canned_replies.html", {"replies": CannedReply.objects.select_related("created_by")})


@staff_only
def legal_refer(request, pk):
    """Step 1 of 'Send to legal': confirm the referral (and why, if it skips the usual process). Nothing is emailed."""
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype"), pk=pk)
    if v.status == S.REFERRED_LEGAL:
        return redirect("legal_email", pk=pk)
    after_fine = v.status == S.FINE_APPROVED
    if request.method == "POST":
        note = (request.POST.get("note") or "").strip()
        if not after_fine and not note:
            messages.error(request, "Say why this is going straight to the attorney.")
        else:
            if _run(request, services.refer_to_attorney, v, request.user, note) is None and v.status != S.REFERRED_LEGAL:
                v.refresh_from_db()
            if v.status == S.REFERRED_LEGAL:
                messages.success(request, "Marked as referred to the attorney. Review the draft email below; nothing has been sent.")
                return redirect("legal_email", pk=pk)
    flags = [x for x, on in (("Non curable violation", v.non_curable), ("Safety issue", v.safety_issue), ("Board declared emergency", v.board_emergency)) if on]
    return render(request, "violations/legal_refer.html", {"v": v, "after_fine": after_fine, "flags": flags,
                                                           "prefill": request.POST.get("note", "") or ("; ".join(flags) if flags else "")})


@staff_only
def legal_email(request, pk):
    """Step 2: review and edit the drafted email to the attorney, then send it, or download everything to send from your own email."""
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype"), pk=pk)
    if v.status != S.REFERRED_LEGAL:
        messages.info(request, "This case has not been referred to the attorney.")
        return redirect(v)
    draft = services.legal_email_draft(v, request.user)
    if request.method == "POST":
        P = request.POST
        to, cc, subject, body = P.get("to", "").strip(), P.get("cc", "").strip(), P.get("subject", ""), P.get("body", "")
        keys = P.getlist("att")
        for a in draft["attachments"]:
            a["checked"] = a["key"] in keys
        draft.update(to=to, subject=subject, body=body)
        if P.get("do") == "download":
            import io, zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("email-draft.txt", f"To: {to}\nCc: {cc}\nSubject: {subject}\n\n{body}\n")
                for k in keys:
                    att = services.legal_attachment_bytes(v, k)
                    if att:
                        z.writestr(att[0], att[1])
            return HttpResponse(buf.getvalue(), content_type="application/zip",
                                headers={"Content-Disposition": f'attachment; filename="legal-package-case-{v.pk}.zip"'})
        if P.get("do") == "send":
            if _run(request, services.send_legal_email, v, to, subject, body, keys, request.user, cc, ok=f"Sent to {to}.") is not None or v.legal_sent_on:
                v.refresh_from_db()
                if v.legal_sent_on:
                    return redirect(v)
    return render(request, "violations/legal_email.html", {"v": v, "draft": draft, "cc": request.POST.get("cc", "")})


@staff_only
def owner_correspondence(request, pk):
    """Board / manager: every letter and email sent to this owner (and tenant) across all their cases, with mailing and tracking details."""
    o = get_object_or_404(Owner, pk=pk)
    cases = [{"v": v, "mail": services.correspondence(v, staff=True)} for v in o.violations.exclude(status=S.DRAFT).select_related("vtype")]
    return render(request, "violations/owner_correspondence.html", {"owner": o, "cases": cases})


def _mail_flags(v):
    """What the collapsed "Mailing log" card must still tell staff at a glance: letters to mail, certified letters not delivered, mail on hold."""
    pieces = list(MailPiece.objects.filter(notice__violation=v))
    return {"to_mail": sum(1 for p in pieces if p.to_mail), "problems": sum(1 for p in pieces if p.needs_attention),
            "held": sum(1 for n in v.notices.all() if n.mail_held)}


@staff_only
def violation_detail(request, pk):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype", "committee_meeting"), pk=pk)
    v.responses.filter(from_resident=True, read_at__isnull=True).update(read_at=timezone.now())   # opening the case marks owner messages as read
    ctx = {
        "v": v, "S": S, "mail": services.correspondence(v, staff=True),
        "extension_form": forms.ExtensionForm(), "reply_form": forms.ReplyForm(), "canned": CannedReply.objects.all(),
        "mailed_form": forms.MailedForm(),
        "board_form": forms.BoardForm(initial={"proposed_fine": v.suggested_fine} if v.is_point_in_time else None),
        "assign_form": forms.AssignForm(), "committee_form": forms.CommitteeForm(),
        "hold_form": forms.HoldForm(),
        "payment_form": forms.PaymentForm(), "note_form": forms.NoteForm(),
        "approve_form": forms.ExtApproveForm(), "decline_form": forms.ExtDeclineForm(),
        "impose_form": forms.ImposeForm(), "ledger_configured": ledger.configured(),
        "history": services.owner_history(v),
        "sv": sitevisits.panel(request.user, v, request, back="case"),
        "sent_letters": v.sent_letters.select_related("sent_by"),
        "no_copy": [n for n in v.notices.all() if not n.sent_copies.exists()],
        "piece_problems": [(k, text[1]) for k, text in MailPiece.PROBLEMS.items()], "today_iso": date.today().isoformat(),
        "mail_flags": _mail_flags(v),
    }
    return render(request, "violations/detail.html", ctx)


@staff_only
def sent_letter(request, pk):
    """Open the exact copy of a letter that was sent (Board and manager only). Refuses a file that no longer matches its fingerprint."""
    s = get_object_or_404(SentLetter, pk=pk)
    try:
        data = s.read_bytes()
    except Exception:       # noqa: BLE001
        return render(request, "violations/sent_letter_problem.html", {"s": s, "why": "The saved file could not be read from storage."}, status=404)
    if hashlib.sha256(data).hexdigest() != s.sha256:
        return render(request, "violations/sent_letter_problem.html", {"s": s, "why": "The saved file no longer matches its fingerprint, so it may have been changed."}, status=409)
    return HttpResponse(data, content_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="case-{s.violation_id}-{s.kind}-{s.sent_at:%Y-%m-%d}.pdf"', "Cache-Control": "private, max-age=300"})


# ---------- site-visit observations (committee, Board, manager only; never owners) ----------
def _site_visit_back(request, v):
    """Where to return after adding an observation: only ever one of our own pages."""
    if request.POST.get("back") == "case" and accounts.role_of(request.user) in STAFF:
        return reverse("violation_detail", args=[v.pk]) + "#site-visits"
    if v.committee_meeting_id:
        return reverse("committee_meeting", args=[v.committee_meeting_id]) + f"#case-{v.pk}"
    return reverse("violation_detail", args=[v.pk]) if accounts.role_of(request.user) in STAFF else reverse("committee_home")


@staff_or_committee
@require_POST
def site_visit_add(request, pk):
    """Add a site-visit observation (a note and/or photographs) or a correction to an earlier one. Never edits or deletes anything."""
    v = get_object_or_404(Violation, pk=pk)
    why = sitevisits.blocked_reason(request.user, v)
    if why:
        sitevisits.log_access(request.user, v, sitevisits.A.DENIED, request, detail=f"tried to add: {why}")
        raise PermissionDenied
    bad = []
    try:
        sitevisits.add(request.user, v, request.POST.get("comment", ""), request.FILES.getlist("photos"), request.POST.get("corrects"), request, bad)
        messages.success(request, "Site-visit observation saved. It can't be edited or deleted; add a correction if something needs fixing.")
    except services.WorkflowError as e:
        messages.error(request, str(e))
    _report_rejected(request, bad)
    return redirect(_site_visit_back(request, v))


@staff_or_committee
def site_visit_photo(request, pk):
    """Open one site-visit photograph (the original, exactly as uploaded). Refuses a file that no longer matches its fingerprint."""
    p = get_object_or_404(SiteVisitPhoto.objects.select_related("observation__violation"), pk=pk)
    v = p.observation.violation
    why = sitevisits.blocked_reason(request.user, v)
    if why:
        sitevisits.log_access(request.user, v, sitevisits.A.DENIED, request, observation=p.observation, photo=p, detail=f"tried to open a photo: {why}")
        raise PermissionDenied
    try:
        data = p.read_bytes()
    except Exception:       # noqa: BLE001
        return HttpResponse("This photograph could not be read from storage.", status=404, content_type="text/plain")
    if hashlib.sha256(data).hexdigest() != p.sha256:
        return HttpResponse("This photograph no longer matches its fingerprint, so it may have been changed.", status=409, content_type="text/plain")
    sitevisits.log_photo_open(request.user, p, request)
    return uploads.file_response(p.file, f"site-visit-{p.observation_id}-{p.pk}", private=True, data=data)


@staff_only
def case_file(request, pk):
    """One zip with everything about the case (Board and manager only). Every download is recorded in the case history."""
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype", "committee_meeting"), pk=pk)
    services.log(v, "Case file downloaded", request.user)
    with_site_visits = sitevisits.allowed(request.user, v)       # not for someone whose own violation it is
    data, name = casefile.build_case_file(v, request.user, site_visits=with_site_visits)
    sitevisits.log_access(request.user, v, sitevisits.A.EXPORTED, request,
                          detail="site-visit observations included" if with_site_visits else "site-visit observations NOT included (own violation)")
    return HttpResponse(data, content_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@staff_only
@require_POST
def violation_action(request, pk, action):
    v = get_object_or_404(Violation, pk=pk)
    u, P = request.user, request.POST
    if action == "add_photos":
        bad = []
        n = _save_photos(v, request.FILES.getlist("photos"), rejected=bad)
        services.log(v, f"{n} photo(s) added", u)
        messages.success(request, f"{n} photo(s) added.")
        _report_rejected(request, bad)
    elif action == "delete_photo":
        _run(request, services.delete_photo, v, get_object_or_404(Photo, pk=_int(P.get("photo")), violation=v), u, ok="Photo removed.")
    elif action == "send_notice":
        _run(request, services.issue_first_notice, v, u, timing=P.get("timing") or None,
             ok="Notice issued: emailed now; the mailed copy follows per the chosen timing.")
    elif action == "refresh_tracking":
        n = services.refresh_mail_tracking(force_for=v)
        messages.success(request, f"Tracking checked: {n} update{'s' if n != 1 else ''}." if settings.MAIL_PROVIDER == "lob"
                         else "Tracking updates need the print/mail provider (Lob) turned on. Until then, log tracking numbers by hand under Mailing log.")
    elif action == "mail_now":
        n = get_object_or_404(Notice, pk=_int(P.get("notice")), violation=v)
        _run(request, services.mail_now, n, u, ok="Mailed copy released.")
    elif action == "reinspect":
        _run(request, services.mark_reinspect, v, u, P.get("note", ""), bool(P.get("notify")), ok="Marked for re-inspection.")
    elif action == "reinspect_result":
        _run(request, services.reinspect_result, v, P.get("result") == "corrected", u, P.get("note", ""),
             ok="Re-inspection recorded.")
    elif action == "cure":
        _run(request, services.mark_cured, v, u, P.get("note", ""), ok="Marked corrected.")
    elif action == "dismiss":
        _run(request, services.dismiss, v, u, P.get("note", ""), ok="Dismissed.")
    elif action == "hold":
        f = forms.HoldForm(P)
        if f.is_valid():
            _run(request, services.put_on_hold, v, f.cleaned_data["reason"], f.cleaned_data["until"], u, ok="Case put on hold.")
        else:
            messages.error(request, " ".join(e for errs in f.errors.values() for e in errs))
    elif action == "resume":
        _run(request, services.resume, v, u, ok="Hold lifted.")
    elif action == "reply":
        f = forms.ReplyForm(P)
        if f.is_valid():
            services.add_response(v, f.cleaned_data["message"], from_resident=False, user=u)
            messages.success(request, f"Reply sent (the owner sees it in the {settings.APP_NAME}).")
            title = (P.get("save_title") or "").strip()
            if P.get("save_reply") and title:
                CannedReply.objects.create(title=title[:80], body=f.cleaned_data["message"], created_by=u)
                messages.success(request, f'Saved as a standard response: "{title[:80]}". The whole team can use it.')
            elif P.get("save_reply"):
                messages.warning(request, "Give the standard response a short title to keep it.")
    elif action == "extension":
        f = forms.ExtensionForm(P)
        if f.is_valid():
            d = f.cleaned_data
            _run(request, services.grant_extension, v, None if d["days"] else d["new_deadline"], u, d["reason"],
                 second_notice=d["second_notice"], days=d["days"], ok="More time granted and the owner notified.")
        else:
            messages.error(request, "Enter the days to add, or an exact new deadline.")
    elif action in ("ext_approve", "ext_decline"):
        resp = Response.objects.filter(pk=_int(P.get("response_id")), violation=v, extension_requested=True, handled=False).first()
        if not resp:
            messages.error(request, "That request has already been answered.")
        elif action == "ext_approve":
            f = forms.ExtApproveForm(P)
            if f.is_valid():
                d = f.cleaned_data
                _run(request, services.grant_extension, v, None if d["days"] else d["new_deadline"], u, d["note"],
                     second_notice=d["second_notice"], days=d["days"], response=resp,
                     ok=f"Approved. New cure deadline {v.cure_deadline:%b %d, %Y}; the owner has been notified.")
                v.refresh_from_db()
            else:
                messages.error(request, "Enter the days to add, or an exact new deadline.")
        else:
            _run(request, services.decline_extension, v, resp, P.get("reason", ""), u, ok="Request declined; the owner has been told.")
        return redirect(_safe_next(request) or v)
    elif action == "board":
        f = forms.BoardForm(P)
        if f.is_valid():
            d = f.cleaned_data
            if d["decision"] == "extend":
                if not (d["extension_days"] or d["new_deadline"]):
                    messages.error(request, "Enter the days to add, or an exact new deadline.")
                else:
                    _run(request, services.grant_extension, v, None if d["extension_days"] else d["new_deadline"], u,
                         f"board {d['meeting_date']}", days=d["extension_days"], ok="Board granted more time.")
            else:
                _run(request, services.board_decide, v, d["decision"], d["meeting_date"], u, d["note"],
                     fine=d["proposed_fine"], by_majority=d["by_majority"],
                     ok="Board decision recorded. It will go to the next Compliance Committee meeting you assign.")
        return redirect(_safe_next(request) or v)
    elif action == "assign":
        f = forms.AssignForm(P)
        if f.is_valid():
            m = f.cleaned_data["meeting"]
            if _run(request, services.assign_to_meeting, v, m, u,
                    ok="Sent to the Compliance Committee; hearing notice emailed and sent for certified mailing.") and services.short_notice(m):
                messages.warning(request, f"This is under the recommended {settings.HEARING_NOTICE_RECOMMENDED_DAYS} days' notice "
                                          f"(the legal minimum is {settings.HEARING_NOTICE_DAYS}). Mail delays can eat into it.")
        else:
            messages.error(request, "Choose an upcoming committee meeting (create one under Meetings).")
    elif action == "committee":
        _committee_vote(request, v, forms.CommitteeForm(P))
    elif action == "impose":
        f = forms.ImposeForm(P)
        if f.is_valid():
            n = _run(request, services.finalize_imposition, v, u, f.cleaned_data["method"], f.cleaned_data["reference"])
            if n is not None:
                messages.success(request, "Approved. " + ("Fine posted to the ledger. " if v.fine_amount else "")
                                 + "The Notice of Imposition was sent to the owner" + (" and tenant" if (v.owner.tenant_is_emailed or v.owner.tenant_is_mailed) else "") + ".")
    elif action == "urgent":
        _run(request, services.refer_urgent, v, u, P.get("note", ""), ok="Referred to the board as a safety matter.")
    elif action == "payment":
        f = forms.PaymentForm(P)
        if f.is_valid():
            _run(request, services.record_payment, v, f.cleaned_data["paid_on"], u, ok="Payment recorded.")
    elif action == "legal":
        return redirect("legal_refer", pk=v.pk)   # the referral has its own step now (see legal_refer)
    return redirect(v)


def _committee_vote(request, v, f):
    if f.is_valid():
        d = f.cleaned_data
        _run(request, services.committee_decide, v, d["votes_for"], d["votes_against"], request.user, d["note"],
             findings=d["findings"], cure_by=d["cure_by"], ok="Committee decision recorded.")
    else:
        messages.error(request, "Enter the votes for and against and the findings of fact (they are quoted in the decision letter).")


@staff_only
@require_POST
def notice_mailed(request, pk):
    notice = get_object_or_404(Notice, pk=pk)
    f = forms.MailedForm(request.POST)
    if f.is_valid():
        d = f.cleaned_data
        services.mark_mailed(notice, d["mailed_on"], d["tracking_number"], request.user, d["method"],
                             d["return_receipt_on"])
        messages.success(request, "Mailing logged.")
    return redirect(notice.violation)


@staff_only
@require_POST
def mail_piece_action(request, pk, action):
    """Board / manager: record what happened to one letter that is mailed by hand (or clear a delivery problem). Actions: mailed (date and
    certified tracking number), delivered, not_delivered (why), handled (what was done about a problem, or 'not needed' for a letter
    never mailed). Every action goes in the case history."""
    piece = get_object_or_404(MailPiece.objects.select_related("notice__violation"), pk=pk)
    form_class = {"mailed": forms.PieceMailedForm, "delivered": forms.PieceDeliveredForm, "not_delivered": forms.PieceNotDeliveredForm,
                  "handled": forms.PieceHandledForm}.get(action)
    if form_class is None:
        raise Http404
    u, f = request.user, form_class(request.POST)
    if not f.is_valid():
        messages.error(request, "Please fill in every box shown for that step (the date, and the tracking number or note where asked).")
    else:
        d = f.cleaned_data
        if action == "mailed":
            _run(request, services.record_piece_mailed, piece, d["mailed_on"], d["tracking_number"], u, ok="Mailing recorded.")
        elif action == "delivered":
            _run(request, services.record_piece_delivered, piece, d["delivered_on"], u, ok="Delivery recorded.")
        elif action == "not_delivered":
            _run(request, services.record_piece_not_delivered, piece, d["reason"], d["on"], d["note"], u,
                 ok="Recorded. It now shows at the top of the Board and manager home screen until someone marks it handled.")
        else:
            _run(request, services.record_piece_handled, piece, d["note"], u, ok="Saved. It no longer shows on the home screen.")
    return redirect(_safe_next(request) or (piece.notice.violation.get_absolute_url() + "#mailing"))


@role_required(*STAFF, "committee", "homeowner")
def notice_pdf(request, pk):
    notice = get_object_or_404(Notice.objects.select_related("violation"), pk=pk)
    if not can_view(request.user, notice.violation):
        raise PermissionDenied
    to = request.GET.get("to") if request.GET.get("to") in ("tenant", "owner_property") and accounts.role_of(request.user) in STAFF else "owner"
    return HttpResponse(render_notice_pdf(notice, to), content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="case-{notice.violation_id}-{notice.kind}.pdf"'})


@staff_only
@require_POST
def board_bulk(request):
    """Record Board decisions for the ticked rows of the queue in one go. Each row carries its own decision, fine and meeting date
    (the toolbar just fills them in), so any row can differ."""
    P = request.POST
    ids = [i for i in (_int(x, -1) for x in P.getlist("pick")) if i > 0]
    if not ids:
        messages.error(request, "Tick the violations you want to act on.")
        return redirect("board_queue")
    by_majority = bool(P.get("by_majority"))
    done, problems = 0, []
    for v in Violation.objects.filter(pk__in=ids, status=S.PENDING_BOARD).select_related("owner"):
        try:
            when = date.fromisoformat(P.get(f"date_{v.pk}") or P.get("date") or "")
        except ValueError:
            problems.append(f"#{v.pk}: enter the Board meeting date.")
            continue
        decision = P.get(f"decision_{v.pk}", "proceed")
        try:
            if decision == "extend":
                days = _int(P.get("days"))
                if days <= 0:
                    raise services.WorkflowError("Enter the days to add.")
                services.grant_extension(v, None, request.user, f"board {when}", days=days)
            else:
                fine = P.get(f"fine_{v.pk}") or None
                services.board_decide(v, decision, when, request.user, P.get("note", ""), fine=fine, by_majority=by_majority)
            done += 1
        except (services.WorkflowError, ValueError, ArithmeticError) as e:
            problems.append(f"#{v.pk} {v.owner.property_address}: {e}")
    if done:
        messages.success(request, f"Recorded {done} Board decision{'s' if done != 1 else ''}. Fines issued go to Waiting to be scheduled; assign them to a committee meeting.")
    for pr in problems:
        messages.error(request, pr)
    return redirect("board_queue")


@staff_only
def board_queue(request):
    services.advance_expired()
    vs = list(Violation.objects.filter(status=S.PENDING_BOARD).select_related("owner", "vtype").prefetch_related("photos", "responses", "notices"))
    pool = {}       # every owner's other cases, loaded once for the whole list (not one set of queries per case)
    for o in (Violation.objects.filter(owner_id__in={v.owner_id for v in vs}).exclude(status=S.DRAFT).select_related("vtype").order_by("-created_at")):
        pool.setdefault(o.owner_id, []).append(o)
    for v in vs:  # the Board may adopt a schedule of fines: start from this violation type's usual fine
        v.bform = forms.BoardForm(initial={"proposed_fine": v.suggested_fine})
        v.history = services.owner_history(v, pool=pool)
        v.peek = next((p for p in v.photos.all() if not p.from_resident), None)
        v.pending_ext = next((r for r in v.responses.all() if r.extension_requested and not r.handled), None)     # the oldest unanswered request (same as pending_extension)
        v.unread = any(r.from_resident and r.read_at is None for r in v.responses.all())
    return render(request, "violations/board.html", {"today": date.today(), "verify_list": _verify_list(), "ledger_pending": Violation.objects.filter(status=S.LEDGER_PENDING).select_related("owner", "vtype"),
                                                      "violations": vs, "board_form": forms.BoardForm(),
                                                      "approve_form": forms.ExtApproveForm(), "decline_form": forms.ExtDeclineForm(),
                                                      "need_hearing": Violation.objects.filter(status=S.HEARING_NEEDED)})


# ---------- Compliance Committee meetings ----------
@staff_only
def meetings(request):
    form = forms.MeetingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        extra = dict(zoom_meeting_id=d["zoom_meeting_id"], zoom_passcode=d["zoom_passcode"], zoom_dial_in=d["zoom_dial_in"])
        first = None
        if d["repeat"]:
            made, err = services.create_meeting_series(d["starts_at"], int(d["repeat_months"] or 12), request.user, d["zoom_link"], d["location"],
                                                       create_zoom=d["create_zoom"], **extra)
            if made:
                first = made[0]
                messages.success(request, f"Created {len(made)} meeting{'s' if len(made) != 1 else ''} ({made[0].series}).")
            if err:
                messages.error(request, f"Stopped at {err}")
        else:
            first = _run(request, services.create_meeting, d["starts_at"], d["zoom_link"], d["location"], request.user,
                         create_zoom=d["create_zoom"], ok="Meeting created.", **extra)
        if first and services.short_notice(first):
            messages.warning(request, f"The first meeting is under {settings.HEARING_NOTICE_RECOMMENDED_DAYS} days away. {settings.HEARING_NOTICE_DAYS} days' notice is "
                                      f"required, and the extra days allow for printing and mailing the hearing notices.")
        if first:
            return redirect("meetings")
    from . import zoom
    upcoming = list(CommitteeMeeting.objects.filter(starts_at__gte=timezone.now()).annotate(n=Count("violations")).order_by("starts_at"))
    today = timezone.localdate()
    for m in upcoming:
        m.days_away = (timezone.localtime(m.starts_at).date() - today).days
        m.short = m.days_away < settings.HEARING_NOTICE_RECOMMENDED_DAYS
    return render(request, "violations/meetings.html", {
        "zoom_ready": zoom.configured(), "form": form, "upcoming": upcoming,
        "past": CommitteeMeeting.objects.filter(starts_at__lt=timezone.now()).annotate(n=Count("violations"))[:8],
        "waiting": Violation.objects.filter(status=S.HEARING_NEEDED).select_related("owner", "vtype"),
        "recommended": settings.HEARING_NOTICE_RECOMMENDED_DAYS, "minimum": settings.HEARING_NOTICE_DAYS})


@staff_only
def meeting_manage(request, pk):
    """One meeting: its details and agenda, and the way to move or cancel it."""
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    if request.method == "POST":
        act = request.POST.get("action")
        notify = bool(request.POST.get("notify"))
        if act == "reschedule":
            try:
                start = forms.MeetingForm.base_fields["starts_at"].clean(request.POST.get("starts_at"))
            except Exception:
                messages.error(request, "Enter the new date and time.")
                return redirect("meeting_manage", pk=pk)
            warns = _run(request, services.reschedule_meeting, m, start, request.user, notify, ok="Meeting moved.")
            for w in warns or []:
                messages.warning(request, w)
        elif act == "cancel":
            n = m.violations.count()
            warns = _run(request, services.cancel_meeting, m, request.user, notify, ok="Meeting cancelled." + (f" {n} violation{'s' if n != 1 else ''} went back to waiting for a hearing." if n else ""))
            if warns is not None:
                for w in warns:
                    messages.warning(request, w)
                return redirect("meetings")
        return redirect("meeting_manage", pk=pk)
    cases = m.violations.select_related("owner", "vtype").order_by("owner__property_address")
    locked = m.results_status != CommitteeMeeting.Results.OPEN or any(v.committee_outcome or v.status != S.HEARING_SCHEDULED for v in cases)
    return render(request, "violations/meeting_manage.html", {"m": m, "cases": cases, "locked": locked,
                                                              "days_away": (timezone.localtime(m.starts_at).date() - timezone.localdate()).days,
                                                              "minimum": settings.HEARING_NOTICE_DAYS, "recommended": settings.HEARING_NOTICE_RECOMMENDED_DAYS})


@staff_only
@require_POST
def meeting_assign_selected(request):
    """Pick a future meeting from the list and assign the ticked waiting cases to it."""
    m = get_object_or_404(CommitteeMeeting, pk=_int(request.POST.get("meeting")))
    ids = [i for i in (_int(x, -1) for x in request.POST.getlist("pick")) if i > 0]
    cases = list(Violation.objects.filter(pk__in=ids, status=S.HEARING_NEEDED))
    if not cases:
        messages.error(request, "Tick at least one waiting violation.")
        return redirect("meetings")
    done, skipped = services.assign_all_to_meeting(m, request.user, cases=cases)
    if done:
        messages.success(request, f"{len(done)} violation{'s' if len(done) != 1 else ''} assigned to the {timezone.localtime(m.starts_at):%B} "
                                  f"{timezone.localtime(m.starts_at).day} meeting; hearing notices emailed and sent for certified mailing.")
        if services.short_notice(m):
            messages.warning(request, f"This meeting is under the recommended {settings.HEARING_NOTICE_RECOMMENDED_DAYS} days' notice "
                                      f"(the legal minimum is {settings.HEARING_NOTICE_DAYS}). Mail delays can eat into it.")
    for v, why in skipped:
        messages.error(request, f"Case #{v.pk} not added: {why}")
    return redirect("meetings")


@staff_only
@require_POST
def meeting_assign_all(request, pk):
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    done, skipped = services.assign_all_to_meeting(m, request.user)
    messages.success(request, f"{len(done)} case(s) sent to the committee; hearing notices emailed and sent for certified mailing.")
    if done and services.short_notice(m):
        messages.warning(request, f"This meeting is under the recommended {settings.HEARING_NOTICE_RECOMMENDED_DAYS} days' notice "
                                  f"(the legal minimum is {settings.HEARING_NOTICE_DAYS}). Mail delays can eat into it.")
    for v, why in skipped:
        messages.error(request, f"Case #{v.pk} not added: {why}")
    return redirect("meetings")


@staff_or_committee
def committee_home(request):
    now = timezone.now()
    ms = CommitteeMeeting.objects.annotate(n=Count("violations"), done=Count("violations", filter=~Q(violations__committee_outcome="")))
    return render(request, "violations/committee_home.html", {
        "waiting": Violation.objects.filter(status=S.HEARING_NEEDED).select_related("owner", "vtype").order_by("board_meeting_date", "pk"),
        "minutes_waiting": CommitteeMeeting.objects.filter(minutes_status="draft", starts_at__lt=now).order_by("starts_at"),
        "upcoming": ms.filter(starts_at__gte=now - timedelta(hours=12)).order_by("starts_at"),
        "past": ms.filter(starts_at__lt=now - timedelta(hours=12))[:6]})


def _meeting_summary(m):
    """Counts and dollars for a committee meeting, from the Board's final decision where there is one, else the committee's."""
    cases = list(m.violations.select_related("owner", "vtype").order_by("owner__property_address"))
    fines = waived = rejected = pending = 0
    total = 0
    for v in cases:
        out = v.board_outcome or v.committee_outcome
        amt = v.board_final_fine if v.board_outcome else (v.committee_amended_fine if v.committee_outcome == "amend" else v.proposed_fine)
        if not out:
            pending += 1
        elif out in ("confirm", "amend"):
            fines += 1
            total += amt or 0
        elif out == "waive":
            waived += 1
        else:
            rejected += 1
    return {"cases": cases, "n": len(cases), "fines": fines, "waived": waived, "rejected": rejected, "undecided": pending, "total": total}


def _minutes_waiting(m):
    """Earlier meetings whose draft minutes this meeting can approve."""
    return list(CommitteeMeeting.objects.filter(starts_at__lt=m.starts_at, minutes_status=CommitteeMeeting.Minutes.DRAFT)
                .exclude(pk=m.pk).filter(starts_at__lt=timezone.now()).order_by("starts_at"))


@staff_only
def owner_archive(request):
    """Former owners and tenants' records (moved out / sold), kept for ARCHIVE_RETENTION_YEARS and searchable here. Nothing is lost:
    their violations, letters and emails stay attached to them."""
    if request.method == "POST":
        o = get_object_or_404(Owner, pk=_int(request.POST.get("id")), active=False)
        if Owner.objects.filter(property_address=o.property_address, active=True).exists():
            messages.error(request, f"{o.property_address} already has an active owner, so this record can't be reactivated.")
        else:
            o.active = True
            o.save()
            accounts.sync_account(o)
            audit.record("owner.reactivated", o.property_address, o.name, request=request)
            messages.success(request, f"{o.name} is active again.")
        return redirect("owner_archive")
    q = (request.GET.get("q") or "").strip()
    qs = Owner.objects.filter(active=False).annotate(n=Count("violations"))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(second_owner_name__icontains=q) | Q(property_address__icontains=q) | Q(lot_no__iexact=q)
                       | Q(email__icontains=q) | Q(additional_emails__icontains=q) | Q(tenant_name__icontains=q) | Q(account_number__icontains=q)
                       | Q(external_id__iexact=q))
    return render(request, "violations/owner_archive.html", {"owners": qs.order_by("-inactive_since", "property_address"), "q": q,
                                                              "years": settings.ARCHIVE_RETENTION_YEARS})


@staff_or_committee
def committee_members(request):
    """The committee roster by year. Managers and board members load it once a year; committee members can read it."""
    from django.utils import timezone
    year = timezone.localdate().year
    year = _int(request.GET.get("year"), year)
    staff = accounts.role_of(request.user) in STAFF
    if request.method == "POST":
        if not staff:
            raise PermissionDenied
        P, act = request.POST, request.POST.get("action")
        y = _int(P.get("year"), year)
        login = lambda: get_user_model().objects.filter(username__iexact=(P.get("login") or "").strip()).first() if (P.get("login") or "").strip() else None
        pick = (P.get("owner_pick") or "").strip()
        if act == "add" and pick:                                 # picked from the owner list
            pk, _, which = pick.partition(":")
            o = Owner.objects.filter(pk=_int(pk), active=True).first()
            if o is None:
                messages.error(request, "Pick an owner from the list.")
            else:
                second = which == "2" and o.second_owner_name
                name = o.second_owner_name if second else o.name
                email = (o.co_owner_email if second else o.email) or o.email
                user = accounts.ensure_login(o, email, name, "co_owner" if second else "owner")
                if CommitteeMember.objects.filter(year=y, owner=o, name=name).exists():
                    messages.error(request, f"{name} is already on the {y} roster.")
                else:
                    CommitteeMember.objects.create(name=name, role=P.get("role") if P.get("role") in ("chair", "secretary", "member") else "member",
                                                   year=y, owner=o, user=user)
                    audit.record("roster.added", f"{name} ({y})", f"role: {P.get('role') or 'member'}; an owner at {o.property_address}", request=request)
                    messages.success(request, f"{name} added." + ("" if user else " They have no email on file, so they have no login yet: fix it in Vantaca."))
        elif (P.get("login") or "").strip() and login() is None:
            messages.error(request, f'There is no login named "{P["login"].strip()}". Create it first (Admin, Users) or leave this blank.')
        elif act == "add" and (P.get("name") or "").strip():
            CommitteeMember.objects.create(name=P["name"].strip()[:120], role=P.get("role") if P.get("role") in ("chair", "secretary", "member") else "member",
                                           year=y, user=login())
            audit.record("roster.added", f"{P['name'].strip()[:120]} ({y})", f"role: {P.get('role') or 'member'}; login: {(P.get('login') or '').strip() or 'none'}", request=request)
            messages.success(request, "Member added.")
        elif act == "update":
            audit.record("roster.changed", f"roster entry {P.get('id')}", f"name: {(P.get('name') or '').strip()[:120]}; active: {bool(P.get('active'))}; "
                         f"role: {P.get('role')}; login: {(P.get('login') or '').strip() or 'none'}", request=request)
            CommitteeMember.objects.filter(pk=_int(P.get("id"))).update(name=(P.get("name") or "").strip()[:120] or "Unnamed", active=bool(P.get("active")),
                                                                      role=P.get("role") if P.get("role") in ("chair", "secretary", "member") else "member", user=login())
            messages.success(request, "Saved.")
        elif act == "delete":
            gone = CommitteeMember.objects.filter(pk=_int(P.get("id"))).first()
            if gone:
                audit.record("roster.removed", f"{gone.name} ({gone.year})", "", request=request)
            CommitteeMember.objects.filter(pk=_int(P.get("id"))).delete()
            messages.success(request, "Removed.")
        elif act == "copy":
            n = 0
            for old in CommitteeMember.objects.filter(year=y - 1, active=True):
                _, new = CommitteeMember.objects.get_or_create(year=y, name=old.name, defaults={"role": old.role, "user": old.user, "owner": old.owner})
                n += int(new)
            audit.record("roster.copied", f"{y - 1} -> {y}", f"{n} member(s) copied", request=request)
            messages.success(request, f"Copied {n} member{'s' if n != 1 else ''} from {y - 1}.")
        accounts.sync_committee_logins()
        return redirect(f"{reverse('committee_members')}?year={y}")
    listed = set(CommitteeMember.objects.filter(year=year, active=True).exclude(user=None).values_list("user_id", flat=True))
    stray = [p.user for p in Profile.objects.filter(role="committee", user__is_active=True).select_related("user") if p.user_id not in listed]
    people = []
    if staff:
        taken = set(CommitteeMember.objects.filter(year=year).values_list("owner_id", "name"))
        for o in Owner.objects.filter(active=True).order_by("name"):
            people.append((f"{o.pk}:1", f"{o.name} ({o.property_address})", (o.pk, o.name) in taken))
            if o.second_owner_name:
                people.append((f"{o.pk}:2", f"{o.second_owner_name} ({o.property_address})", (o.pk, o.second_owner_name) in taken))
    return render(request, "violations/committee_members.html", {"year": year, "members": CommitteeMember.objects.filter(year=year).select_related("user", "owner"), "staff": staff,
                                                                  "people": [p for p in people if not p[2]],
                                                                  "stray_logins": stray,
                                                                  "years": sorted(set(CommitteeMember.objects.values_list("year", flat=True)) | {year, year + 1})})


@staff_or_committee
def committee_minutes(request, pk):
    """The minutes the app wrote from the committee's entries (HTML, or ?pdf=1)."""
    from . import minutes
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    if request.GET.get("pdf"):
        return HttpResponse(minutes.render_pdf(m), content_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="committee-minutes-{timezone.localtime(m.starts_at):%Y-%m-%d}.pdf"'})
    return render(request, "violations/committee_minutes.html", {"m": m, "sections": minutes.build(m)})


@staff_or_committee
@require_POST
def minutes_approve(request, pk):
    """Approve the draft minutes of meeting `pk` at a later meeting (`at`)."""
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    at = get_object_or_404(CommitteeMeeting, pk=_int(request.POST.get("at")))
    if m.minutes_status == CommitteeMeeting.Minutes.APPROVED or at.starts_at <= m.starts_at:
        messages.error(request, "These minutes can't be approved here.")
    else:
        m.minutes_status, m.minutes_approved_on = CommitteeMeeting.Minutes.APPROVED, timezone.localdate()
        m.minutes_approved_by, m.minutes_approved_at = request.user, at
        m.save()
        messages.success(request, f"Minutes of the {timezone.localtime(m.starts_at):%b} {timezone.localtime(m.starts_at).day} meeting approved.")
    return redirect("committee_meeting", pk=at.pk)


@staff_or_committee
def committee_meeting(request, pk):
    """The committee's working page for a meeting: meeting details and minutes, then one decision per violation."""
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    summary = _meeting_summary(m)
    can_edit = m.results_status == CommitteeMeeting.Results.OPEN
    present_ids = set(m.attendance.filter(status="present").values_list("member_id", flat=True))
    rows = [{"v": v, "outcomes": Violation.OUTCOMES, "editable": can_edit and v.status == S.HEARING_SCHEDULED and not owns(request.user, v),
             "mine": owns(request.user, v), "conflicts": services.conflicted_members(v),
             "abstained": {a.member_id for a in v.abstentions.all()},
             "voters": [x for x in m.roster() if x.pk in present_ids or not present_ids],
             "pit": v.owner.violations.filter(non_curable=True).exclude(pk=v.pk).exclude(status=S.DRAFT).count(),
             "sv": sitevisits.panel(request.user, v, request, back="meeting")} for v in summary["cases"]]
    roster = m.roster()
    have = {a.member_id: a.status for a in m.attendance.all()}
    for member in roster:
        member.attended = have.get(member.pk, "")
    return render(request, "violations/committee_meeting.html", {"m": m, "rows": rows, "S": S, "sum": summary, "can_edit": can_edit,
                                                                  "roster": roster, "present_count": sum(1 for x in have.values() if x == "present"),
                                                                  "waiting_minutes": _minutes_waiting(m),
                                                                  "minutes_open": m.minutes_status == CommitteeMeeting.Minutes.DRAFT, "minutes_files": list(m.minutes_files.all()),
                                                                  "ready": can_edit and summary["n"] and not summary["undecided"]})


def _parse_time(s):
    from datetime import time
    try:
        h, mi = (s or "").split(":")[:2]
        return time(int(h), int(mi))
    except ValueError:
        return None


def _meeting_label(m):
    t = timezone.localtime(m.starts_at)
    return f"Meeting {t:%b} {t.day}, {t.year}"


@staff_or_committee
@require_POST
def committee_meeting_save(request, pk):
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    if m.minutes_status == CommitteeMeeting.Minutes.APPROVED:
        messages.error(request, "These minutes were already approved, so the entries are locked.")
        return redirect("committee_meeting", pk=pk)
    P = request.POST
    m.attendees, m.notes = P.get("attendees", "").strip(), P.get("notes", "").strip()
    m.members_absent, m.guests = P.get("members_absent", "").strip(), P.get("guests", "").strip()
    for member in m.roster():                                   # the roster tick-boxes: present / absent / excused (blank clears)
        val = P.get(f"attend_{member.pk}", "")
        if val in ("present", "absent", "excused"):
            MeetingAttendance.objects.update_or_create(meeting=m, member=member, defaults={"status": val})
        else:
            MeetingAttendance.objects.filter(meeting=m, member=member).delete()
    m.called_to_order, m.adjourned = _parse_time(P.get("called_to_order")), _parse_time(P.get("adjourned"))
    m.save()
    f = request.FILES.get("minutes")
    if f:
        row, err = minutesfiles.add(m, f, request.user)         # the upload history is the MinutesFile rows (who, when, fingerprint, what it replaced)
        if err:
            messages.error(request, err)
        else:
            audit.record("minutes.uploaded", _meeting_label(m), f"{row.original_name} ({row.size:,} bytes, fingerprint {row.sha256[:12]})"
                         + (", replacing the earlier file" if row.replaces_id else ""), request=request)
    messages.success(request, "Meeting details saved.")
    return redirect("committee_meeting", pk=pk)


def _back_to_meeting(v):
    """Where to send someone after a committee action on case `v`: its meeting page, or (a case that is on no meeting) the committee home."""
    if v.committee_meeting_id:
        return redirect(reverse("committee_meeting", args=[v.committee_meeting_id]) + f"#case-{v.pk}")
    return redirect("committee_home")


@staff_or_committee
@require_POST
def committee_case_record(request, pk):
    v = get_object_or_404(Violation, pk=pk)
    if not can_view(request.user, v):
        raise PermissionDenied
    if not v.committee_meeting_id:
        messages.error(request, "This case is not on a committee meeting, so there is nothing to record yet.")
        return _back_to_meeting(v)
    P = request.POST
    if owns(request.user, v) or any(m.user_id == request.user.pk for m in services.conflicted_members(v)):
        messages.error(request, "This is your own violation (or your household's), so you abstain: you can't record the committee's decision on it.")
        return _back_to_meeting(v)

    def num(k):
        try:
            return int(P.get(k)) if P.get(k) not in (None, "") else None
        except ValueError:
            return None
    amount = P.get("amount") or None
    _run(request, services.committee_record, v, P.get("outcome", ""), request.user, amount, num("votes_for"), num("votes_against"),
         P.get("findings", ""), bool(P.get("owner_present")), P.getlist("abstain"), ok=f"Saved decision for {v.owner.property_address}.")
    return _back_to_meeting(v)


@staff_or_committee
@require_POST
def committee_submit(request, pk):
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    cases = _run(request, services.submit_meeting_results, m, request.user)
    if cases:
        messages.success(request, f"Submitted {len(cases)} result(s) for administrative review.")
    return redirect("committee_meeting", pk=pk)


@staff_or_committee
def meeting_minutes(request, pk):
    """The uploaded minutes (the current file, or an earlier one with ?v=<id>), always as a download. Committee, Board and manager only."""
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    vid = request.GET.get("v")
    if vid:
        row = get_object_or_404(m.minutes_files, pk=_int(vid, -1))
    else:
        row = minutesfiles.current(m)
    if row is not None:
        try:
            data = row.read_bytes()
        except Exception:       # noqa: BLE001  (file missing or storage unreachable)
            raise Http404
        if hashlib.sha256(data).hexdigest() != row.sha256:
            return HttpResponse("This file no longer matches its saved fingerprint, so it is not being sent. Tell the manager.", status=409,
                                content_type="text/plain")
        return minutesfiles.serve(m, row.file, row.kind, data=data, version=row.pk if vid else "")
    if not m.minutes:                                               # a file uploaded before the history existed
        raise Http404
    ext = m.minutes.name.rsplit(".", 1)[-1].lower()
    return minutesfiles.serve(m, m.minutes, ext if ext in minutesfiles.CONTENT_TYPES else "bin")


@staff_only
def results_list(request):
    """Board / manager: every Compliance Committee meeting and what came of it, newest first."""
    rows = []
    for m in CommitteeMeeting.objects.all():
        rows.append({"m": m, **_meeting_summary(m)})
    return render(request, "violations/committee_results_list.html", {"rows": rows})


@staff_only
def results_detail(request, pk):
    """The single record of a committee meeting: details, minutes, and for each violation the original fine, the committee's
    recommendation and the Board's final decision. Where results are waiting, the Board reviews each item here."""
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    summary = _meeting_summary(m)
    return render(request, "violations/committee_results.html", {"m": m, "sum": summary, "S": S,
                                                                  "review": m.results_status == CommitteeMeeting.Results.SUBMITTED,
                                                                  "impose_form": forms.ImposeForm()})


@staff_only
@require_POST
def results_review(request, pk):
    v = get_object_or_404(Violation, pk=pk)
    if not v.committee_meeting_id:
        messages.error(request, "This case is not on a committee meeting, so there is nothing to review.")
        return redirect("results_list")
    P = request.POST
    _run(request, services.board_review_item, v, request.user, P.get("action", ""), P.get("final", ""), P.get("amount") or None,
         P.get("note", ""), P.get("method", "manual"), P.get("reference", ""))
    v.refresh_from_db()
    if v.status != S.LEDGER_PENDING:   # it moved on (approved, waived, rejected or returned); otherwise an error message was already shown
        messages.success(request, f"{v.owner.property_address}: {v.get_status_display()}.")
    return redirect("results_detail", pk=v.committee_meeting_id)


@staff_or_committee
def committee_case(request, pk):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype", "committee_meeting"), pk=pk)
    if not can_view(request.user, v):
        raise PermissionDenied
    return render(request, "violations/committee_case.html", {
        "v": v, "S": S, "committee_form": forms.CommitteeForm(), "history": services.owner_history(v),
        "can_vote": v.status == S.HEARING_SCHEDULED and v.hearing_date and v.hearing_date <= timezone.now()})


@staff_only      # Board and manager only: a committee member records the decision on the meeting page, where the meeting-level review applies
@require_POST
def committee_vote(request, pk):
    v = get_object_or_404(Violation, pk=pk)
    if not can_view(request.user, v):
        raise PermissionDenied
    if owns(request.user, v) or any(m.user_id == request.user.pk for m in services.conflicted_members(v)):      # same abstention rule as the meeting page
        messages.error(request, "This is your own violation (or your household's), so you abstain: you can't record the committee's decision on it.")
        return redirect("committee_case", pk=pk)
    _committee_vote(request, v, forms.CommitteeForm(request.POST))
    return redirect("committee_case", pk=pk)


# ---------- photos ----------
@role_required(*STAFF, "committee", "homeowner")
def photo_file(request, pk):
    p = get_object_or_404(Photo.objects.select_related("violation"), pk=pk)
    if not can_view(request.user, p.violation):
        raise Http404            # the same answer as "no such photo", so photo numbers can't be probed
    return uploads.photo_response(p)


# ---------- homeowner portal (login) ----------
def _handle_owner_post(request, v, form):
    d = form.cleaned_data
    if not v.is_open:
        messages.error(request, "This case is closed.")
        return
    services.add_response(v, d["message"], from_resident=True, user=request.user if request.user.is_authenticated else None,
                          extension_requested=d["request_extension"],
                          extension_days=d["extension_days"] if d["request_extension"] else None,
                          cure_reported=d["cure_reported"])
    bad = []
    n = _save_photos(v, request.FILES.getlist("photos"), from_resident=True, rejected=bad)
    if n:
        services.log(v, f"Owner uploaded {n} photo(s)")
    messages.success(request, "Thank you — your message has been sent to the association.")
    _report_rejected(request, bad)


def _owner_updates(vs):
    """Recent things for the owner to notice (new notices, messages from the Association), newest first."""
    updates = []
    for v in vs:
        for n in v.notices.all():
            updates.append({"when": n.created_at, "text": f"{n.get_kind_display()}: {v.vtype.name}", "v": v,
                            "new": n.kind in ("violation", "second", "hearing", "fine")})
        for r in v.responses.all():
            if not r.from_resident:
                updates.append({"when": r.created_at, "text": f"New message from the Association: {v.vtype.name}", "v": v, "new": True})
    updates.sort(key=lambda u: u["when"], reverse=True)
    cutoff = timezone.now() - timedelta(days=7)
    for u in updates:
        u["fresh"] = u["when"] >= cutoff
    return updates[:6]


def _owner_hearings(owner):
    """[(meeting, [this owner's violations on its agenda], days_left)] for hearings that haven't happened yet. Only the owner's own cases."""
    if owner is None:
        return []
    cases = (Violation.objects.filter(owner=owner, status=S.HEARING_SCHEDULED, committee_meeting__isnull=False,
                                      committee_meeting__starts_at__gte=timezone.now() - timedelta(hours=3))
             .select_related("vtype", "committee_meeting").order_by("committee_meeting__starts_at", "pk"))
    grouped = {}
    for v in cases:
        grouped.setdefault(v.committee_meeting_id, (v.committee_meeting, []))[1].append(v)
    today = timezone.localdate()
    return [(m, vs, (timezone.localtime(m.starts_at).date() - today).days) for m, vs in grouped.values()]


@homeowner_only
def my_hearing(request, pk):
    """The owner's view of one hearing: when and how to attend, and ONLY their own violations on that agenda."""
    o = accounts.owner_of(request.user)
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    cases = list(Violation.objects.filter(owner=o, committee_meeting=m).exclude(status=S.DRAFT).select_related("vtype").order_by("pk")) if o else []
    if not cases:
        raise Http404
    days = (timezone.localtime(m.starts_at).date() - timezone.localdate()).days
    return render(request, "violations/me_hearing.html", {"m": m, "cases": cases, "days": days, "past": m.starts_at < timezone.now()})


@homeowner_only
def my_home(request):
    """The owner's front page: a tile for each part of the portal, plus what's new."""
    from . import portal
    o = accounts.owner_of(request.user)
    vs = list(Violation.objects.filter(owner=o).exclude(status=S.DRAFT).select_related("vtype").prefetch_related("notices", "responses")) if o else []
    return render(request, "violations/me_home.html", {"owner": o, "tiles": portal.owner_tiles(request.user, o), "updates": _owner_updates(vs),
                                                       "hearings": _owner_hearings(o)})


@homeowner_only
def my_violations(request):
    """The owner's violations: each open one with its photo, issued date and cure date, nearest cure date first."""
    o = accounts.owner_of(request.user)
    vs = list(Violation.objects.filter(owner=o).exclude(status=S.DRAFT).select_related("vtype").prefetch_related("photos", "notices")) if o else []
    today = date.today()
    cards = []
    for v in vs:
        photo = next((p for p in v.photos.all() if not p.from_resident), None)
        first = next((n for n in v.notices.all() if n.kind == "violation"), None)
        left = (v.cure_deadline - today).days if v.cure_deadline and v.status == S.NOTICE_SENT else None
        cards.append({"v": v, "photo": photo, "issued": (first.created_at if first else v.created_at).date(), "left": left,
                      "open": v.status in Violation.OPEN})
    for c in cards:
        c["closed"] = c["v"].owner_closed
    open_cards = sorted([c for c in cards if not c["closed"]], key=lambda c: (c["v"].cure_deadline is None, c["v"].cure_deadline or today, c["v"].pk))
    past = sorted([c for c in cards if c["closed"]], key=lambda c: c["issued"], reverse=True)
    return render(request, "violations/me_list.html", {"owner": o, "open_cards": open_cards, "past_cards": past})


@homeowner_only
def my_violation(request, pk):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype"), pk=pk)
    if not owns(request.user, v):           # staff and committee roles must not reach other owners' cases through the owner pages
        raise Http404
    form = forms.ResidentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _handle_owner_post(request, v, form)
        return redirect("my_violation", pk=pk)
    return render(request, "violations/me_detail.html", {"v": v, "form": form, "S": S,
                                                         "notices": v.notices.all(), "mail": services.correspondence(v),
                                                         "latest_letter": next((n for n in reversed(list(v.notices.all())) if n.kind in ("violation", "second")), None),
                                                         "state_url": f"/me/{pk}/state/"})


@homeowner_only
def my_password(request):
    """Optional: an owner can set a password to sign in with, as well as the emailed link. They can also reset it themselves
    ('Forgot your password?' on the sign-in page). Changing the *email* is not possible here: that comes from the management company."""
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import SetPasswordForm
    form = SetPasswordForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        update_session_auth_hash(request, request.user)
        messages.success(request, "Password saved. You can now sign in with your email and this password, or with an emailed link.")
        return redirect("my_password")
    return render(request, "violations/me_password.html", {"form": form, "email": request.user.email})


@homeowner_only
@require_GET
def my_household(request):
    """READ-ONLY. Who has access and the contact details on file. Residents can't change any of it here: corrections go through
    the property manager and come into this tool from Vantaca, so there is a single source of truth."""
    o = accounts.owner_of(request.user)
    if o is None:
        raise Http404
    mine = o.profiles.filter(user=request.user).first()
    return render(request, "violations/me_household.html", {
        "owner": o, "members": accounts.household_logins(o),
        "is_tenant": bool(mine and mine.relationship == "tenant")})  # tenants see their own details, not the owner's


@homeowner_only
def my_violation_state(request, pk):
    v = get_object_or_404(Violation, pk=pk)
    if not owns(request.user, v):
        raise Http404
    return JsonResponse({"sig": f"{v.status}:{v.notices.count()}:{v.responses.count()}:{v.photos.count()}"})


# ---------- resident via emailed private link (no login) ----------
TOKEN_STAGES = (S.NOTICE_SENT, S.ON_HOLD)  # the emailed/mailed link only works during the cure period


def resident(request, token):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype"), token=token)
    if v.status == S.DRAFT:
        raise Http404
    if v.status not in TOKEN_STAGES:  # board/committee stage and beyond: require a real login
        return render(request, "violations/resident_locked.html", {"v": v})
    form = forms.ResidentForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        # anyone holding this link can post, so the number of posts is limited (per link, and per visitor address)
        if throttle.hit(f"resident:{v.pk}", RESIDENT_POSTS_PER_HOUR) or throttle.hit(f"resident-ip:{throttle.client_ip(request)}", 30):
            messages.error(request, "You have sent several messages in a short time. Please wait a while and try again; "
                                    "what you typed is still in the box. For anything urgent, contact the association directly.")
            status = 429
        else:
            _handle_owner_post(request, v, form)
            return redirect("resident", token=token)
    resp = render(request, "violations/resident.html", {"v": v, "form": form}, status=status)
    resp["Referrer-Policy"] = "no-referrer"       # the private address is never passed on to other sites
    resp["Cache-Control"] = "no-store, private"   # the page shows the case without a login: a shared computer's browser must not keep it
    return resp


def resident_photo(request, token, pk):
    p = get_object_or_404(Photo, pk=pk, violation__token=token, violation__status__in=TOKEN_STAGES)
    resp = uploads.photo_response(p)
    resp["Referrer-Policy"] = "no-referrer"
    return resp


# ---------- map ----------
RESOLVED = (S.CURED, S.CLOSED, S.DISMISSED, S.FINE_REJECTED, S.WAIVED)
SERIOUS = (S.PENDING_BOARD, S.HEARING_NEEDED, S.HEARING_SCHEDULED, S.LEDGER_PENDING, S.FINE_APPROVED, S.REFERRED_LEGAL)

# Stages offered as map filters (same steps as the progress tracker, plus the two flags).
MAP_STAGES = [("cure", "Notice issued / cure period"), ("board", "Board review / fine issued by Board"),
              ("committee", "Sent to Compliance Committee / hearing"), ("imposed", "Fine imposed & posted to ledger"),
              ("hold", "On hold"), ("legal", "Sent to legal"), ("resolved", "Resolved / closed")]
STAGE_OF = {S.NOTICE_SENT: "cure", S.PENDING_BOARD: "board", S.HEARING_NEEDED: "board", S.HEARING_SCHEDULED: "committee",
            S.LEDGER_PENDING: "imposed", S.FINE_APPROVED: "imposed", S.ON_HOLD: "hold", S.REFERRED_LEGAL: "legal",
            S.CURED: "resolved", S.CLOSED: "resolved", S.DISMISSED: "resolved", S.FINE_REJECTED: "resolved", S.WAIVED: "resolved"}


def _issued_on(v):
    """When the violation was issued: the first Notice of Violation, else when it was recorded."""
    n = next((n for n in v.notices.all() if n.kind == "violation"), None)
    return timezone.localtime(n.created_at if n else v.created_at).date()


def map_data():
    # Every active home, with or without street coordinates: the Site plan goes by lot number, the street / satellite maps need lat/lng.
    owners = Owner.objects.filter(active=True).prefetch_related("violations__vtype", "violations__notices")
    out = []
    for o in owners:
        vs = [v for v in o.violations.all() if v.status != S.DRAFT]
        active = [v for v in vs if v.status not in RESOLVED]
        if any(v.status in SERIOUS for v in active):
            color = "red"
        elif active:
            color = "orange"
        elif vs:
            color = "green"
        else:
            color = "gray"
        out.append({"id": o.pk, "address": o.property_address, "owner": o.name, "lot": o.lot_no, "lat": o.lat, "lng": o.lng, "rented": o.has_tenant,
                    "color": color, "open": len(active), "resolved": len(vs) - len(active),
                    "cases": [{"id": v.pk, "type": v.vtype.name, "status": v.get_status_display(), "url": v.get_absolute_url(),
                               "resolved": v.status in RESOLVED, "stage": STAGE_OF[v.status], "date": _issued_on(v).isoformat()}
                              for v in vs]})
    return out


def _spread(homes):
    """Units in one building share a coordinate: fan them out in a small ring so every home is its own dot."""
    import math
    groups = {}
    for h in homes:
        if h["lat"] is None or h["lng"] is None:
            continue
        groups.setdefault((round(h["lat"], 5), round(h["lng"], 5)), []).append(h)
    for hs in groups.values():
        if len(hs) > 1:
            r = 0.00006 * (1 + len(hs) / 6)  # about 7-10 m: units in one building separate once you zoom in
            for i, h in enumerate(sorted(hs, key=lambda x: x["address"])):
                a = 2 * math.pi * i / len(hs)
                h["lat"] += r * math.sin(a)
                h["lng"] += r * math.cos(a)
    return homes


def _site_plan():
    """Where each lot number sits on the site-plan image (see tools/build_site_plan.py); {} if the file is missing."""
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parent / "static" / "violations" / "site-plan-lots.json"
    return json.loads(f.read_text()) if f.exists() else {"width": 1, "height": 1, "lots": {}}


@staff_only
def map_page(request):
    return render(request, "violations/map.html", {
        "missing": Owner.objects.filter(lat__isnull=True, active=True).count(),
        "stages": MAP_STAGES, "ranges": [30, 60, 90, 180], "plan": _site_plan(),
        "tiles": {"url": settings.MAP_TILE_URL, "attr": settings.MAP_TILE_ATTRIBUTION,
                  "fallback_url": settings.MAP_TILE_FALLBACK_URL, "fallback_attr": settings.MAP_TILE_FALLBACK_ATTRIBUTION,
                  "sat_url": settings.MAP_SATELLITE_URL, "sat_attr": settings.MAP_SATELLITE_ATTRIBUTION}})


@staff_only
def map_api(request):
    return JsonResponse({"homes": _spread(map_data())})


# ---------- importing the owner list, and putting homes on the map ----------


@staff_only
def import_owners(request):
    """Import owners: 1) upload, 2) match columns, 3) preview, 4) import. The uploaded rows wait in the session between steps."""
    from .models import ImportMapping
    sess = request.session
    ctx = {"form": forms.ImportForm(), "fields": vantaca.MAPPABLE}
    if request.method == "POST":
        action = request.POST.get("action", "upload")
        if action == "cancel":
            sess.pop("import", None)
            return redirect("import_owners")
        if action == "upload":
            form = forms.ImportForm(request.POST, request.FILES)
            ctx["form"] = form
            if not form.is_valid():
                return render(request, "violations/import.html", ctx)
            f = request.FILES["file"]
            if f.size > MAX_IMPORT_BYTES:
                form.add_error("file", "That file is too large (the limit is 10 MB). An owner list is normally well under 1 MB: check it is the right file.")
                return render(request, "violations/import.html", ctx)
            try:
                rows = vantaca.rows_from_upload(f.read(), f.name)
            except vantaca.UploadError as e:
                form.add_error("file", str(e))
                return render(request, "violations/import.html", ctx)
            headers = list(rows[0].keys())
            for r in rows:  # keep any column that appears later than the first row
                headers += [k for k in r if k not in headers]
            sess["import"] = {"rows": rows, "headers": headers, "name": f.name}
        imp = sess.get("import")
        if not imp:
            messages.error(request, "Your upload expired. Please upload the file again.")
            return redirect("import_owners")
        headers, rows = imp["headers"], imp["rows"]
        if action == "upload":  # first time: use the saved matching for these headers, else guess from the names
            saved = ImportMapping.objects.filter(signature=vantaca.header_signature(headers)).first()
            mapping = saved.mapping if saved else vantaca.auto_mapping(headers)
            ctx.update(step="map", mapping=mapping, remembered=bool(saved))
        else:
            mapping = {h: request.POST.get(f"map_{i}", "") for i, h in enumerate(headers)}
            probs = vantaca.mapping_problems(mapping)
            if action == "map" and probs:
                ctx.update(step="map", mapping=mapping, map_problems=probs)
            else:
                ImportMapping.objects.update_or_create(signature=vantaca.header_signature(headers), defaults={"mapping": mapping})
                mapped = vantaca.apply_mapping(rows, mapping)
                if action == "apply":
                    result = vantaca.sync_rows(mapped, invite=bool(request.POST.get("invite")), actor=request.user)
                    sess.pop("import", None)
                    summary = ", ".join(f"{v} {k}" for k, v in result.items() if v)
                    if result.get("failed"):
                        messages.warning(request, f"Imported: {summary}. {result['failed']} row(s) could not be imported: see the Audit log for which rows and why.")
                    else:
                        messages.success(request, "Imported: " + summary)
                    return redirect("import_owners")
                _, result, problems = vantaca.preview_rows(mapped)
                ctx.update(step="preview", mapping=mapping, preview=result, problems=vantaca.group_problems(problems), n=len(rows))
        if ctx.get("step") == "map":
            samples = {h: [r[h] for r in rows if r.get(h)][:3] for h in headers}
            ctx.update(columns=[(i, h, samples[h], ctx["mapping"].get(h, "")) for i, h in enumerate(headers)], n=len(rows),
                       filename=imp["name"])
    return render(request, "violations/import.html", ctx)


@staff_only
def import_template(request):
    cols = ["Homeowner ID", "Account Number", "Homeowner(s)", "Address", "Mailing Address", "Street No", "Address 1", "City", "State",
            "Zip", "First Name", "Last Name", "Second Owner First Name", "Second Owner Last Name", "Mailing Address 1", "Lot No",
            "Phone", "Email", "Tenant Name(s)", "Tenant Email(s)", "Tenant Phone(s)", "Tenant Min Start Date", "Tenant Min End Date"]
    ex = ["H1001", "A-1001", "Pat Owner", "7335 Great Egret Blvd #52", "PO Box 5, Tampa, FL 33601", "", "", "Sarasota", "FL", "34241",
          "Pat", "Owner", "Sam", "Owner", "", "52", "941-555-0100", "pat@example.com;pat.work@example.com", "", "", "", "", ""]
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    _csv.writer(buf).writerows([cols, ex])
    return HttpResponse(buf.getvalue(), content_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="heron-landing-owner-template.csv"'})


@staff_only
@require_POST
def locate_homes(request):
    try:
        located, missing = geocoding.geocode_missing()
    except Exception as e:  # service down / blocked network: say so instead of crashing
        messages.error(request, f"Couldn't reach the address lookup service ({type(e).__name__}). Try again later.")
        return redirect("map")
    messages.success(request, f"{located} home(s) placed on the map." + (f" {missing} could not be found: check those addresses." if missing else ""))
    return redirect("map")


# ---------- logins, sign-in email changes, two-step resets, audit log (Board / manager only) ----------
@staff_only
def owner_logins(request, pk):
    """Board / manager: the sign-in logins of one household, with the controlled 'Change sign-in email' and 'Sign out everywhere' actions."""
    o = get_object_or_404(Owner, pk=pk)
    if request.method == "POST":
        profile = get_object_or_404(o.profiles.select_related("user"), pk=_int(request.POST.get("profile")))
        try:
            if request.POST.get("action") == "change_email":
                r = accounts.change_signin_email(profile, request.POST.get("new_email", ""), request.user, request.POST.get("reason", ""), request)
                msg = (f"Sign-in email changed from {r['old'] or '(none)'} to {r['new']}. Old links and sessions were cancelled. "
                       + ("The old address was told. " if r["notice_sent"] else "The old address could NOT be emailed (see the audit log). ")
                       + ("A first sign-in link was sent to the new address." if r["link_sent"] else "The sign-in link could NOT be sent (see the audit log)."))
                messages.success(request, msg + " Remember: if Vantaca still has the old address, its next sync will change it back, so update Vantaca too.")
            elif request.POST.get("action") == "sign_out":
                n = accounts.end_sessions(profile.user)
                audit.record("sessions.ended", f"{o.property_address} ({profile.user.get_username()})", f"Signed out everywhere ({n} session(s)).", request=request)
                messages.success(request, f"{profile.user.get_username()} was signed out everywhere ({n} session{'s' if n != 1 else ''}).")
        except accounts.AccountError as e:
            messages.error(request, str(e))
        return redirect("owner_logins", pk=o.pk)
    logins = [{"p": p, "u": p.user} for p in o.profiles.select_related("user").order_by("pk")]
    trail = AuditLog.objects.filter(subject__startswith=o.property_address)[:30]
    return render(request, "violations/owner_logins.html", {"owner": o, "logins": logins, "trail": trail})


@staff_only
def staff_logins(request):
    """Board / manager: every Board, manager and committee login, with two-step status and the audited 'Reset two-step' action."""
    from django_otp import user_has_device
    if request.method == "POST":
        target = get_object_or_404(get_user_model().objects.select_related("profile"), pk=_int(request.POST.get("user")))
        try:
            if request.POST.get("action") == "reset_two_step":
                r = accounts.reset_two_step(target, request.user, request.POST.get("reason", ""), request)
                messages.success(request, f"Two-step sign-in reset for {target.get_username()}: they were signed out everywhere"
                                          f"{' and emailed' if r['emailed'] else ' (the email could not be sent)'}. They set it up again at their next sign-in.")
            elif request.POST.get("action") == "sign_out":
                n = accounts.end_sessions(target)
                audit.record("sessions.ended", target.get_username(), f"Signed out everywhere ({n} session(s)).", request=request)
                messages.success(request, f"{target.get_username()} was signed out everywhere ({n} session{'s' if n != 1 else ''}).")
        except accounts.AccountError as e:
            messages.error(request, str(e))
        return redirect("staff_logins")
    rows = []
    for u in get_user_model().objects.filter(profile__role__in=("manager", "board", "committee")).select_related("profile", "profile__owner").order_by("profile__role", "username"):
        rows.append({"u": u, "role": u.profile.get_role_display().split(" (")[0], "device": user_has_device(u), "me": u.pk == request.user.pk})
    return render(request, "violations/staff_logins.html", {"rows": rows})


@staff_only
def audit_log(request):
    """Board / manager: the account and record audit trail (newest first). Look only."""
    q = (request.GET.get("q") or "").strip()
    qs = AuditLog.objects.all()
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(subject__icontains=q) | Q(detail__icontains=q) | Q(actor_name__icontains=q))
    return render(request, "violations/audit_log.html", {"entries": qs[:300], "q": q, "total": AuditLog.objects.count()})
