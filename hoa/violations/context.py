from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from . import accounts


def _staff_counts():
    """The numbers shown under the top menu headings (one query each, per page)."""
    from .models import CommitteeMeeting, Owner, Response, Violation
    S = Violation.Status
    c = Violation.objects.exclude(status=S.DRAFT).aggregate(
        total=Count("id"), open=Count("id", filter=Q(status__in=[x for x in Violation.OPEN if x != S.DRAFT])),
        board=Count("id", filter=Q(status=S.PENDING_BOARD)), hearing=Count("id", filter=Q(status=S.HEARING_NEEDED)),
        ledger=Count("id", filter=Q(status=S.LEDGER_PENDING)))
    asks = Response.objects.filter(handled=False, violation__status__in=Violation.OPEN).filter(
        Q(extension_requested=True) | Q(cure_reported=True)).count()
    upcoming = CommitteeMeeting.objects.filter(starts_at__gte=timezone.now() - timedelta(hours=12)).count()
    waiting_on_board = c["board"] + c["hearing"] + c["ledger"]
    return {"open": c["open"], "total": c["total"], "board": waiting_on_board,
            "board_tip": f"{c['board']} awaiting a Board vote, {c['hearing']} waiting for a hearing, {c['ledger']} awaiting approval to post",
            "meetings": upcoming, "homes": Owner.objects.filter(active=True).count(),
            "reinspect": Violation.objects.filter(reinspect_on__isnull=False, status__in=Violation.OPEN).count(),
            "results": Violation.objects.filter(status=S.LEDGER_PENDING, committee_meeting__results_status="submitted").count(),
            "attention": waiting_on_board + asks + Violation.objects.filter(reinspect_on__isnull=False, status__in=Violation.OPEN).count()
            + Violation.objects.filter(status=S.REFERRED_LEGAL, legal_sent_on__isnull=True).count()}


def hoa(request):
    role = accounts.role_of(request.user)
    ctx = {"HOA_NAME": settings.HOA_NAME, "MANAGER_PHONE": settings.OFFICE_PHONE, "MANAGER_EMAIL": settings.MANAGER_EMAIL, "ROLE": role}
    if role in accounts.STAFF_ROLES:
        ctx["NAV"] = _staff_counts()
    elif role == "committee":
        from .models import CommitteeMeeting
        ctx["NAV"] = {"meetings": CommitteeMeeting.objects.filter(starts_at__gte=timezone.now() - timedelta(hours=12)).count()}
    elif role == "homeowner":
        o = accounts.owner_of(request.user)
        if o:
            from .models import Violation
            ctx["NAV"] = {"mine": o.violations.exclude(status="draft").filter(status__in=Violation.OPEN).count()}
        u = request.user
        ctx["RESIDENT_NAME"] = (u.get_full_name() or (o.display_name if o else "") or u.first_name or u.get_username()).strip()
    return ctx
