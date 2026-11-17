from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import forms, services
from .models import Notice, Photo, Response, Violation
from .pdf import render_notice_pdf

S = Violation.Status
MAX_UPLOAD = 10 * 1024 * 1024


def _is_image(f):
    from PIL import Image
    try:
        Image.open(f).verify()
        return True
    except Exception:
        return False
    finally:
        f.seek(0)


def _save_photos(v, files, from_resident=False):
    n = 0
    for f in files:
        if f.size > MAX_UPLOAD or not _is_image(f):
            continue
        p = Photo(violation=v, from_resident=from_resident)
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


# ---------- staff ----------
def _awaiting_reply():
    """Owner messages with no staff reply since; the procedure promises a response within 48-72 hours."""
    later_staff = Response.objects.filter(violation=OuterRef("violation"), from_resident=False,
                                          created_at__gt=OuterRef("created_at"))
    qs = (Response.objects.filter(from_resident=True, violation__status__in=Violation.OPEN)
          .annotate(answered=Exists(later_staff)).filter(answered=False)
          .select_related("violation__owner").order_by("created_at"))
    cutoff = timezone.now() - timedelta(hours=settings.REPLY_TARGET_HOURS)
    return [(r, r.created_at < cutoff) for r in qs]


@login_required
def dashboard(request):
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
        "today": date.today(),
    }
    return render(request, "violations/dashboard.html", ctx)


@login_required
def violation_list(request):
    qs = Violation.objects.select_related("owner", "vtype")
    status, q = request.GET.get("status", ""), request.GET.get("q", "").strip()
    if status == "open":
        qs = qs.filter(status__in=Violation.OPEN)
    elif status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(owner__name__icontains=q) | Q(owner__property_address__icontains=q)
                       | Q(description__icontains=q))
    return render(request, "violations/list.html", {"violations": qs, "status": status, "q": q, "statuses": S.choices})


@login_required
def violation_create(request):
    form = forms.ViolationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        v = form.save(commit=False)
        v.created_by = request.user
        v.save()
        n = _save_photos(v, request.FILES.getlist("photos"))
        services.log(v, f"Violation drafted with {n} photo(s)", request.user)
        return redirect(v)
    return render(request, "violations/form.html", {"form": form})


@login_required
def violation_detail(request, pk):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype"), pk=pk)
    ctx = {
        "v": v, "S": S,
        "extension_form": forms.ExtensionForm(), "reply_form": forms.ReplyForm(),
        "mailed_form": forms.MailedForm(), "board_form": forms.BoardForm(),
        "hearing_form": forms.HearingForm(), "committee_form": forms.CommitteeForm(),
        "payment_form": forms.PaymentForm(), "note_form": forms.NoteForm(),
        "pending_ext": v.responses.filter(extension_requested=True, handled=False).first(),
    }
    return render(request, "violations/detail.html", ctx)


@login_required
@require_POST
def violation_action(request, pk, action):
    v = get_object_or_404(Violation, pk=pk)
    u = request.user
    P = request.POST
    if action == "add_photos":
        n = _save_photos(v, request.FILES.getlist("photos"))
        services.log(v, f"{n} photo(s) added", u)
        messages.success(request, f"{n} photo(s) added.")
    elif action == "send_notice":
        _run(request, services.issue_first_notice, v, u, ok="Notice issued and emailed. Print the PDF and mail it, then log the mailing.")
    elif action == "cure":
        _run(request, services.mark_cured, v, u, P.get("note", ""), ok="Marked cured.")
    elif action == "dismiss":
        _run(request, services.dismiss, v, u, P.get("note", ""), ok="Dismissed.")
    elif action == "reply":
        f = forms.ReplyForm(P)
        if f.is_valid():
            services.add_response(v, f.cleaned_data["message"], from_resident=False, user=u)
            messages.success(request, "Reply recorded (visible to the owner on their page).")
    elif action == "extension":
        f = forms.ExtensionForm(P)
        if f.is_valid():
            _run(request, services.grant_extension, v, f.cleaned_data["new_deadline"], u,
                 f.cleaned_data["reason"], second_notice=f.cleaned_data["second_notice"],
                 ok="New deadline set and notice sent.")
    elif action == "board":
        f = forms.BoardForm(P)
        if f.is_valid():
            d = f.cleaned_data
            if d["decision"] == "extend":
                if not d["new_deadline"]:
                    messages.error(request, "Enter the new cure deadline.")
                else:
                    _run(request, services.grant_extension, v, d["new_deadline"], u, f"board {d['meeting_date']}",
                         ok="Board granted more time.")
            else:
                _run(request, services.board_decide, v, d["decision"], d["meeting_date"], u, d["note"],
                     fine=d["proposed_fine"], suspension=d["proposed_suspension"],
                     ok="Board decision recorded. Schedule the committee hearing next.")
        return redirect(request.POST.get("next") or v)
    elif action == "hearing":
        f = forms.HearingForm(P)
        if f.is_valid():
            _run(request, services.schedule_hearing, v, f.cleaned_data["hearing_date"], f.cleaned_data["location"],
                 u, ok="Hearing scheduled; hearing notice emailed.")
        else:
            messages.error(request, "Enter a valid hearing date and time.")
    elif action == "committee":
        f = forms.CommitteeForm(P)
        if f.is_valid():
            d = f.cleaned_data
            _run(request, services.committee_decide, v, d["votes_for"], d["votes_against"], u, d["note"],
                 ok="Committee decision recorded.")
    elif action == "urgent":
        _run(request, services.refer_urgent, v, u, P.get("note", ""), ok="Referred to the board as a safety matter.")
    elif action == "payment":
        f = forms.PaymentForm(P)
        if f.is_valid():
            _run(request, services.record_payment, v, f.cleaned_data["paid_on"], u, ok="Payment recorded.")
    elif action == "legal":
        _run(request, services.refer_to_attorney, v, u, P.get("note", ""), ok="Referred to the attorney.")
    return redirect(v)


@login_required
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


@login_required
def notice_pdf(request, pk):
    notice = get_object_or_404(Notice, pk=pk)
    to = "tenant" if request.GET.get("to") == "tenant" else "owner"
    return HttpResponse(render_notice_pdf(notice, to), content_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="case-{notice.violation_id}-{notice.kind}.pdf"'})


@login_required
def board_queue(request):
    services.advance_expired()
    vs = Violation.objects.filter(status=S.PENDING_BOARD).select_related("owner", "vtype")
    return render(request, "violations/board.html", {"violations": vs, "board_form": forms.BoardForm(),
                                                      "need_hearing": Violation.objects.filter(status=S.HEARING_NEEDED)})


@login_required
def photo_file(request, pk):
    p = get_object_or_404(Photo, pk=pk)
    return FileResponse(p.image.open("rb"))


# ---------- resident (no login; unguessable token) ----------
def resident(request, token):
    v = get_object_or_404(Violation.objects.select_related("owner", "vtype"), token=token)
    if v.status == S.DRAFT:
        raise Http404
    form = forms.ResidentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if not v.is_open:
            messages.error(request, "This case is closed.")
        else:
            services.add_response(v, d["message"], from_resident=True, extension_requested=d["request_extension"],
                                  extension_days=d["extension_days"] if d["request_extension"] else None,
                                  cure_reported=d["cure_reported"], attorney_notice=d["attorney_notice"])
            n = _save_photos(v, request.FILES.getlist("photos"), from_resident=True)
            if n:
                services.log(v, f"Owner uploaded {n} photo(s)")
            messages.success(request, "Thank you — your response has been sent to the association.")
        return redirect("resident", token=token)
    return render(request, "violations/resident.html", {"v": v, "form": form})


def resident_photo(request, token, pk):
    p = get_object_or_404(Photo, pk=pk, violation__token=token)
    return FileResponse(p.image.open("rb"))
