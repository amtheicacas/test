from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from . import accounts


def _staff_counts():
    """The numbers shown under the top menu headings (one query each, per page)."""
    from .models import CommitteeMeeting, MailPiece, Owner, Response, Violation
    from . import services
    S = Violation.Status
    c = Violation.objects.exclude(status=S.DRAFT).aggregate(
        total=Count("id"), open=Count("id", filter=Q(status__in=[x for x in Violation.OPEN if x != S.DRAFT])),
        board=Count("id", filter=Q(status=S.PENDING_BOARD)), hearing=Count("id", filter=Q(status=S.HEARING_NEEDED)),
        ledger=Count("id", filter=Q(status=S.LEDGER_PENDING)))
    asks = Response.objects.filter(handled=False, violation__status__in=Violation.OPEN).filter(
        Q(extension_requested=True) | Q(cure_reported=True)).count()
    upcoming = CommitteeMeeting.objects.filter(starts_at__gte=timezone.now() - timedelta(hours=12)).count()
    waiting_on_board = c["board"] + c["hearing"] + c["ledger"]
    mail_bad = MailPiece.objects.filter(service="certified", problem=True, delivered_on__isnull=True, handled_on__isnull=True).count()    # could not be delivered
    to_mail = MailPiece.objects.filter(by_hand=True, mailed_on__isnull=True, handled_on__isnull=True).count()                          # still to be mailed by hand
    to_check = services.certified_to_check().count()                                                                                    # certified, no result yet
    return {"open": c["open"], "total": c["total"], "board": waiting_on_board,
            "board_tip": f"{c['board']} awaiting a Board vote, {c['hearing']} waiting for a hearing, {c['ledger']} awaiting approval to post",
            "meetings": upcoming, "homes": Owner.objects.filter(active=True).count(),
            "reinspect": Violation.objects.filter(reinspect_on__isnull=False, status__in=Violation.OPEN).count(),
            "results": Violation.objects.filter(status=S.LEDGER_PENDING, committee_meeting__results_status="submitted").count(),
            "attention": waiting_on_board + asks + Violation.objects.filter(reinspect_on__isnull=False, status__in=Violation.OPEN).count()
            + Violation.objects.filter(status=S.REFERRED_LEGAL, legal_sent_on__isnull=True).count() + mail_bad + to_mail + to_check,
            "mail_problems": mail_bad, "to_mail": to_mail}


def hoa(request):
    role = accounts.role_of(request.user)
    o = accounts.owner_of(request.user)
    ctx = {"STAGING": settings.STAGING, "SUSPENSION_ENABLED": settings.SUSPENSION_ENABLED, "APP_NAME": settings.APP_NAME, "HOA_NAME": settings.HOA_NAME, "MANAGER_PHONE": settings.OFFICE_PHONE, "MANAGER_EMAIL": settings.MANAGER_EMAIL, "ROLE": role, "HOA_DOCUMENTS_URL": settings.HOA_DOCUMENTS_URL,
           "IS_OWNER": o is not None}
    # NAVMODE: which menu to show. An owner who is also on the committee or Board sees the owner menu on their own pages (/me/...) and the
    # committee or Board menu elsewhere, with a link between the two.
    mode = "owner" if (o is not None and (role == "homeowner" or request.path.startswith("/me/") or request.path == "/")) else role
    ctx["NAVMODE"] = mode
    if role in accounts.STAFF_ROLES:
        ctx["NAV"] = _staff_counts()
    elif role == "committee":
        from .models import CommitteeMeeting
        ctx["NAV"] = {"meetings": CommitteeMeeting.objects.filter(starts_at__gte=timezone.now() - timedelta(hours=12)).count()}
    if o is not None:
        from .models import Violation
        ctx["NAV"] = {**ctx.get("NAV", {}), "mine": o.violations.exclude(status="draft").filter(status__in=Violation.OPEN).exclude(status__in=Violation.OWNER_CLOSED).count()}
        u = request.user
        ctx["RESIDENT_NAME"] = (u.get_full_name() or o.display_name or u.first_name or u.get_username()).strip()
    return ctx
