import json
from datetime import date, timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, OuterRef, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import accounts, forms, geocoding, ledger, services, standards_search, vantaca
from .models import CommitteeMeeting, Notice, Owner, Photo, Response, Violation
from .pdf import render_notice_pdf

S = Violation.Status
MAX_UPLOAD = 10 * 1024 * 1024
STAFF = ("manager", "board")
COMMITTEE_VISIBLE = (S.HEARING_SCHEDULED, S.LEDGER_PENDING, S.FINE_APPROVED, S.FINE_REJECTED, S.CLOSED, S.REFERRED_LEGAL)


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


staff_only = role_required(*STAFF)
staff_or_committee = role_required(*STAFF, "committee")
homeowner_only = role_required("homeowner")


def can_view(user, v):
    """Per-case access: staff see all; committee only cases sent to a meeting; homeowners only their own."""
    role = accounts.role_of(user)
    if role in STAFF:
        return True
    if role == "committee":
        return v.committee_meeting_id is not None and v.status in COMMITTEE_VISIBLE
    if role == "homeowner":
        o = accounts.owner_of(user)
        return bool(o) and v.owner_id == o.id and v.status != S.DRAFT
    return False


def _is_image(f):
    from PIL import Image
    try:
        Image.open(f).verify()
        return True
    except Exception:
        return False
    finally:
        f.seek(0)


def _taken_at(f):
    """When the photo was taken, from EXIF (DateTimeOriginal), or None."""
    from datetime import datetime
    from PIL import Image
    try:
        exif = Image.open(f).getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        return timezone.make_aware(datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")) if raw else None
    except Exception:
        return None
    finally:
        f.seek(0)


def _save_photos(v, files, from_resident=False):
    n = 0
    for f in files:
        if f.size > MAX_UPLOAD or not _is_image(f):
            continue
        p = Photo(violation=v, from_resident=from_resident, taken_at=_taken_at(f))
        p.image.save(f.name, f, save=False)
        p.save()
        n += 1
    return n


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


# ---------- landing / dashboard ----------
def home(request):
    if not request.user.is_authenticated:
        return redirect_to_login("/")
    role = accounts.role_of(request.user)
    if role == "homeowner":
        return redirect("my_violations")
    if role == "committee":
        return redirect("committee_home")
    if role not in STAFF:
        raise PermissionDenied
    services.advance_expired()
    counts = dict(Violation.objects.values_list("status").annotate(c=Count("id")))
    ctx = {
        "counts": [(label, counts.get(val, 0), val) for val, label in S.choices],
        "pending_requests": Response.objects.filter(extension_requested=True, handled=False,
                                                    violation__status__in=Violation.OPEN).select_related("violation__owner"),
        "expiring": Violation.objects.filter(status=S.NOTICE_SENT).order_by("cure_deadline")[:8],
        "board_queue": Violation.objects.filter(status=S.PENDING_BOARD).count(),
        "need_hearing": Violation.objects.filter(status=S.HEARING_NEEDED).count(),
        "unmailed": Notice.objects.filter(mailed_on__isnull=True).count(),
        "reinspect": Response.objects.filter(cure_reported=True, handled=False,
                                             violation__status__in=Violation.OPEN).select_related("violation__owner"),
        "awaiting_reply": _awaiting_reply(),
        "unpaid": Violation.objects.filter(status=S.FINE_APPROVED).order_by("fine_due_on"),
        "ledger_pending": Violation.objects.filter(status=S.LEDGER_PENDING).select_related("owner", "vtype"),
        "today": date.today(),
    }
    return render(request, "violations/dashboard.html", ctx)


# ---------- staff / board ----------
@staff_only
def violation_list(request):
    qs = Violation.objects.select_related("owner", "vtype")
    g = request.GET
    status, q, vtype = g.get("status", ""), g.get("q", "").strip(), g.get("type", "")
    if status == "open":
        qs = qs.filter(status__in=Violation.OPEN)
    elif status:
        qs = qs.filter(status=status)
    if vtype:
        qs = qs.filter(vtype_id=vtype)
    if q:
        qs = qs.filter(Q(owner__name__icontains=q) | Q(owner__property_address__icontains=q)
                       | Q(description__icontains=q) | Q(vtype__name__icontains=q))
    if g.get("from"):
        qs = qs.filter(observed_on__gte=g["from"])
    if g.get("to"):
        qs = qs.filter(observed_on__lte=g["to"])
    from .models import ViolationType
    return render(request, "violations/list.html", {"violations": qs, "status": status, "q": q, "statuses": S.choices,
                                                    "types": ViolationType.objects.all(), "vtype": vtype,
                                                    "frm": g.get("from", ""), "to": g.get("to", "")})


@staff_only
def standards_search_api(request):
    """JSON for the violation picker. ?q=text (local search); add &ai=1 to also ask the AI."""
    q = request.GET.get("q", "").strip()[:500]
    local = [standards_search.serialize(t) for t in standards_search.local_search(q)]
    out = {"results": local, "ai": False, "ai_available": standards_search.ai_available()}
    if request.GET.get("ai") == "1" and q:
        ai = standards_search.ai_search(q)
        if ai is None:
            out["ai_note"] = "AI search isn't available right now; showing keyword matches."
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
        v.save()
        n = _save_photos(v, request.FILES.getlist("photos"))
        services.log(v, f"Violation recorded with {n} photo(s)", request.user)
        if form.cleaned_data["send_now"]:
            if _run(request, services.issue_first_notice, v, request.user,
                    ok="Notice issued: emailed and sent to the print/mail provider.") is None:
                messages.info(request, "Saved as a draft instead.")
        return redirect(v)
    from .models import ViolationType
    catalog = [standards_search.serialize(t) for t in ViolationType.objects.exclude(code__isnull=True).order_by("section", "code")]
    return render(request, "violations/form.html", {"form": form, "catalog": catalog,
                                                    "ai_available": standards_search.ai_available(),
                                                    "categories": sorted({c["category"] for c in catalog})})


@staff_only
def violation_detail(request, pk):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype", "committee_meeting"), pk=pk)
    ctx = {
        "v": v, "S": S, "steps": v.stage_steps(),
        "extension_form": forms.ExtensionForm(), "reply_form": forms.ReplyForm(),
        "mailed_form": forms.MailedForm(), "board_form": forms.BoardForm(),
        "assign_form": forms.AssignForm(), "committee_form": forms.CommitteeForm(),
        "hold_form": forms.HoldForm(),
        "payment_form": forms.PaymentForm(), "note_form": forms.NoteForm(),
        "approve_form": forms.ExtApproveForm(), "decline_form": forms.ExtDeclineForm(),
        "impose_form": forms.ImposeForm(), "ledger_configured": ledger.configured(),
    }
    return render(request, "violations/detail.html", ctx)


@staff_only
@require_POST
def violation_action(request, pk, action):
    v = get_object_or_404(Violation, pk=pk)
    u, P = request.user, request.POST
    if action == "add_photos":
        n = _save_photos(v, request.FILES.getlist("photos"))
        services.log(v, f"{n} photo(s) added", u)
        messages.success(request, f"{n} photo(s) added.")
    elif action == "send_notice":
        _run(request, services.issue_first_notice, v, u, ok="Notice issued: emailed and sent to the print/mail provider (or ready to print).")
    elif action == "cure":
        _run(request, services.mark_cured, v, u, P.get("note", ""), ok="Marked corrected.")
    elif action == "dismiss":
        _run(request, services.dismiss, v, u, P.get("note", ""), ok="Dismissed.")
    elif action == "hold":
        f = forms.HoldForm(P)
        if f.is_valid():
            _run(request, services.put_on_hold, v, f.cleaned_data["reason"], u, ok="Case put on hold.")
    elif action == "resume":
        _run(request, services.resume, v, u, ok="Hold lifted.")
    elif action == "reply":
        f = forms.ReplyForm(P)
        if f.is_valid():
            services.add_response(v, f.cleaned_data["message"], from_resident=False, user=u)
            messages.success(request, "Reply sent (the owner sees it in their portal).")
    elif action == "extension":
        f = forms.ExtensionForm(P)
        if f.is_valid():
            d = f.cleaned_data
            _run(request, services.grant_extension, v, None if d["days"] else d["new_deadline"], u, d["reason"],
                 second_notice=d["second_notice"], days=d["days"], ok="More time granted and the owner notified.")
        else:
            messages.error(request, "Enter the days to add, or an exact new deadline.")
    elif action in ("ext_approve", "ext_decline"):
        resp = Response.objects.filter(pk=P.get("response_id") or 0, violation=v, extension_requested=True, handled=False).first()
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
        return redirect(request.POST.get("next") or v)
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
                     fine=d["proposed_fine"], suspension=d["proposed_suspension"], votes_for=d["board_votes_for"],
                     votes_against=d["board_votes_against"], suspension_days=d["proposed_suspension_days"],
                     ok="Board decision recorded. It will go to the next Compliance Committee meeting you assign.")
        return redirect(request.POST.get("next") or v)
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
                                 + "The Notice of Imposition was sent to the owner" + (" and tenant" if v.owner.has_tenant else "") + ".")
    elif action == "urgent":
        _run(request, services.refer_urgent, v, u, P.get("note", ""), ok="Referred to the board as a safety matter.")
    elif action == "payment":
        f = forms.PaymentForm(P)
        if f.is_valid():
            _run(request, services.record_payment, v, f.cleaned_data["paid_on"], u, ok="Payment recorded.")
    elif action == "legal":
        _run(request, services.refer_to_attorney, v, u, P.get("note", ""), ok="Referred to the attorney.")
    return redirect(v)


def _committee_vote(request, v, f):
    if f.is_valid():
        d = f.cleaned_data
        _run(request, services.committee_decide, v, d["votes_for"], d["votes_against"], request.user, d["note"],
             ok="Committee decision recorded.")
    else:
        messages.error(request, "Enter the number of votes for and against.")


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


@role_required(*STAFF, "committee", "homeowner")
def notice_pdf(request, pk):
    notice = get_object_or_404(Notice.objects.select_related("violation"), pk=pk)
    if not can_view(request.user, notice.violation):
        raise PermissionDenied
    to = request.GET.get("to") if request.GET.get("to") in ("tenant", "owner_property") and accounts.role_of(request.user) in STAFF else "owner"
    return HttpResponse(render_notice_pdf(notice, to), content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="case-{notice.violation_id}-{notice.kind}.pdf"'})


@staff_only
def board_queue(request):
    services.advance_expired()
    vs = list(Violation.objects.filter(status=S.PENDING_BOARD).select_related("owner", "vtype"))
    for v in vs:  # the Board may adopt a schedule of fines: start from this violation type's usual fine
        v.bform = forms.BoardForm(initial={"proposed_fine": v.vtype.default_fine})
    return render(request, "violations/board.html", {"ledger_pending": Violation.objects.filter(status=S.LEDGER_PENDING).select_related("owner", "vtype"),
                                                      "violations": vs, "board_form": forms.BoardForm(),
                                                      "approve_form": forms.ExtApproveForm(), "decline_form": forms.ExtDeclineForm(),
                                                      "need_hearing": Violation.objects.filter(status=S.HEARING_NEEDED)})


# ---------- Compliance Committee meetings ----------
@staff_only
def meetings(request):
    form = forms.MeetingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        m = _run(request, services.create_meeting, d["starts_at"], d["zoom_link"], d["location"], request.user,
                 zoom_meeting_id=d["zoom_meeting_id"], zoom_passcode=d["zoom_passcode"], zoom_dial_in=d["zoom_dial_in"],
                 ok="Meeting created.")
        if m:
            return redirect("meetings")
    return render(request, "violations/meetings.html", {
        "form": form, "meetings": CommitteeMeeting.objects.annotate(n=Count("violations")),
        "waiting": Violation.objects.filter(status=S.HEARING_NEEDED).select_related("owner")})


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
    ms = CommitteeMeeting.objects.annotate(n=Count("violations"))
    return render(request, "violations/committee_home.html", {
        "upcoming": ms.filter(starts_at__gte=now - timedelta(hours=12)).order_by("starts_at"),
        "past": ms.filter(starts_at__lt=now - timedelta(hours=12))[:6]})


@staff_or_committee
def committee_meeting(request, pk):
    m = get_object_or_404(CommitteeMeeting, pk=pk)
    cases = m.violations.select_related("owner", "vtype").order_by("owner__property_address")
    return render(request, "violations/committee_meeting.html", {"m": m, "cases": cases, "S": S})


@staff_or_committee
def committee_case(request, pk):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype", "committee_meeting"), pk=pk)
    if not can_view(request.user, v):
        raise PermissionDenied
    return render(request, "violations/committee_case.html", {
        "v": v, "S": S, "steps": v.stage_steps(), "committee_form": forms.CommitteeForm(),
        "can_vote": v.status == S.HEARING_SCHEDULED and v.hearing_date and v.hearing_date <= timezone.now()})


@staff_or_committee
@require_POST
def committee_vote(request, pk):
    v = get_object_or_404(Violation, pk=pk)
    if not can_view(request.user, v):
        raise PermissionDenied
    _committee_vote(request, v, forms.CommitteeForm(request.POST))
    return redirect("committee_case", pk=pk)


# ---------- photos ----------
@role_required(*STAFF, "committee", "homeowner")
def photo_file(request, pk):
    p = get_object_or_404(Photo.objects.select_related("violation"), pk=pk)
    if not can_view(request.user, p.violation):
        raise PermissionDenied
    return FileResponse(p.image.open("rb"))


# ---------- homeowner portal (login) ----------
def _handle_owner_post(request, v, form):
    d = form.cleaned_data
    if not v.is_open:
        messages.error(request, "This case is closed.")
        return
    services.add_response(v, d["message"], from_resident=True, user=request.user if request.user.is_authenticated else None,
                          extension_requested=d["request_extension"],
                          extension_days=d["extension_days"] if d["request_extension"] else None,
                          cure_reported=d["cure_reported"], attorney_notice=d["attorney_notice"])
    n = _save_photos(v, request.FILES.getlist("photos"), from_resident=True)
    if n:
        services.log(v, f"Owner uploaded {n} photo(s)")
    messages.success(request, "Thank you — your message has been sent to the association.")


@homeowner_only
def my_violations(request):
    o = accounts.owner_of(request.user)
    vs = Violation.objects.filter(owner=o).exclude(status=S.DRAFT).select_related("vtype") if o else []
    return render(request, "violations/me_list.html", {"owner": o, "violations": vs})


@homeowner_only
def my_violation(request, pk):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype"), pk=pk)
    if not can_view(request.user, v):
        raise Http404
    form = forms.ResidentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _handle_owner_post(request, v, form)
        return redirect("my_violation", pk=pk)
    return render(request, "violations/me_detail.html", {"v": v, "steps": v.stage_steps(), "form": form, "S": S,
                                                         "notices": v.notices.all(), "state_url": f"/me/{pk}/state/"})


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
    if not can_view(request.user, v):
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
    if request.method == "POST" and form.is_valid():
        _handle_owner_post(request, v, form)
        return redirect("resident", token=token)
    return render(request, "violations/resident.html", {"v": v, "form": form, "steps": v.stage_steps()})


def resident_photo(request, token, pk):
    p = get_object_or_404(Photo, pk=pk, violation__token=token, violation__status__in=TOKEN_STAGES)
    return FileResponse(p.image.open("rb"))


# ---------- map ----------
RESOLVED = (S.CURED, S.CLOSED, S.DISMISSED, S.FINE_REJECTED)
SERIOUS = (S.PENDING_BOARD, S.HEARING_NEEDED, S.HEARING_SCHEDULED, S.FINE_APPROVED, S.REFERRED_LEGAL)


def map_data():
    owners = Owner.objects.exclude(lat__isnull=True).exclude(lng__isnull=True).prefetch_related("violations__vtype")
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
        out.append({"id": o.pk, "address": o.property_address, "owner": o.name, "lat": o.lat, "lng": o.lng, "rented": o.has_tenant,
                    "color": color, "open": len(active), "resolved": len(vs) - len(active),
                    "cases": [{"id": v.pk, "type": v.vtype.name, "status": v.get_status_display(), "url": v.get_absolute_url(),
                               "resolved": v.status in RESOLVED} for v in vs]})
    return out


def _spread(homes):
    """Units in one building share a coordinate: fan them out in a small ring so every home is its own dot."""
    import math
    groups = {}
    for h in homes:
        groups.setdefault((round(h["lat"], 5), round(h["lng"], 5)), []).append(h)
    for hs in groups.values():
        if len(hs) > 1:
            r = 0.00003 * (1 + len(hs) / 6)
            for i, h in enumerate(sorted(hs, key=lambda x: x["address"])):
                a = 2 * math.pi * i / len(hs)
                h["lat"] += r * math.sin(a)
                h["lng"] += r * math.cos(a)
    return homes


@staff_only
def map_page(request):
    return render(request, "violations/map.html", {"missing": Owner.objects.filter(lat__isnull=True, active=True).count()})


@staff_only
def map_api(request):
    return JsonResponse({"homes": _spread(map_data())})


# ---------- importing the owner list, and putting homes on the map ----------
manager_only = role_required("manager")


@manager_only
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
                    result = vantaca.sync_rows(mapped, invite=bool(request.POST.get("invite")))
                    sess.pop("import", None)
                    messages.success(request, "Imported: " + ", ".join(f"{v} {k}" for k, v in result.items() if v))
                    return redirect("import_owners")
                _, result, problems = vantaca.preview_rows(mapped)
                ctx.update(step="preview", mapping=mapping, preview=result, problems=problems, n=len(rows))
        if ctx.get("step") == "map":
            samples = {h: [r[h] for r in rows if r.get(h)][:3] for h in headers}
            ctx.update(columns=[(i, h, samples[h], ctx["mapping"].get(h, "")) for i, h in enumerate(headers)], n=len(rows),
                       filename=imp["name"])
    return render(request, "violations/import.html", ctx)


@manager_only
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


@manager_only
@require_POST
def locate_homes(request):
    try:
        located, missing = geocoding.geocode_missing()
    except Exception as e:  # service down / blocked network: say so instead of crashing
        messages.error(request, f"Couldn't reach the address lookup service ({type(e).__name__}). Try again later.")
        return redirect("map")
    messages.success(request, f"{located} home(s) placed on the map." + (f" {missing} could not be found: check those addresses." if missing else ""))
    return redirect("map")
