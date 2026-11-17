"""All workflow transitions live here so views, the CLI job and tests share one set of rules.

Process (per Fla. Stat. 720.305(2), adjust to your governing documents):
  draft -> notice sent (cure period) -> [cured | extension | expires]
  expires -> awaiting board vote -> [dismiss | extend | proceed]
  proceed -> schedule committee hearing (>= 14 days' notice) -> committee confirms or rejects the fine
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from .models import Event, Notice, Response, Violation
from .pdf import render_notice_pdf

S = Violation.Status


class WorkflowError(Exception):
    pass


def log(v, text, user=None):
    Event.objects.create(violation=v, text=text, user=user)


def _require(v, *allowed):
    if v.status not in allowed:
        raise WorkflowError(f"Not allowed while status is '{v.get_status_display()}'.")


def send_notice(v, kind, user=None, base_deadline=None):
    """Create a notice, email it (if the owner has an email) with the PDF attached. Mailing is logged separately."""
    notice = Notice.objects.create(violation=v, kind=kind)
    if v.owner.email:
        pdf = render_notice_pdf(notice)
        subject = f"{notice.get_kind_display()} — {v.owner.property_address} (Case #{v.pk})"
        body = (f"Dear {v.owner.name},\n\nPlease see the attached {notice.get_kind_display().lower()} regarding "
                f"{v.owner.property_address}.\n\nYou can view details, reply, request more time or upload photos "
                f"of the cure here:\n{v.resident_url()}\n\n{settings.HOA_NAME}")
        msg = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [v.owner.email])
        msg.attach(f"case-{v.pk}-{kind}.pdf", pdf, "application/pdf")
        msg.send()
        notice.emailed_at, notice.emailed_to = timezone.now(), v.owner.email
        notice.save()
        log(v, f"{notice.get_kind_display()} emailed to {v.owner.email}", user)
    else:
        log(v, f"{notice.get_kind_display()} created (no email on file — mail only)", user)
    return notice


@transaction.atomic
def issue_first_notice(v, user=None, today=None):
    _require(v, S.DRAFT)
    if not v.photos.filter(from_resident=False).exists():
        raise WorkflowError("Add at least one photo of the violation before sending.")
    today = today or date.today()
    v.cure_deadline = today + timedelta(days=v.cure_days)
    v.status = S.NOTICE_SENT
    v.save()
    log(v, f"Notice issued; cure deadline {v.cure_deadline:%b %d, %Y}", user)
    return send_notice(v, Notice.Kind.VIOLATION, user)


def mark_mailed(notice, mailed_on, tracking="", user=None):
    notice.mailed_on, notice.tracking_number = mailed_on, tracking
    notice.save()
    log(notice.violation, f"{notice.get_kind_display()} mailed {mailed_on:%b %d, %Y}"
        + (f" (tracking {tracking})" if tracking else ""), user)


@transaction.atomic
def mark_cured(v, user=None, note=""):
    _require(v, *Violation.OPEN)
    v.status = S.CURED
    v.save()
    log(v, "Marked cured" + (f": {note}" if note else ""), user)


@transaction.atomic
def dismiss(v, user=None, note=""):
    _require(v, *Violation.OPEN)
    v.status = S.DISMISSED
    v.save()
    log(v, "Dismissed" + (f": {note}" if note else ""), user)


@transaction.atomic
def grant_extension(v, new_deadline, user=None, reason="", notify=True):
    _require(v, S.NOTICE_SENT, S.PENDING_BOARD)
    if new_deadline <= date.today():
        raise WorkflowError("New deadline must be in the future.")
    old = v.cure_deadline
    v.cure_deadline, v.status = new_deadline, S.NOTICE_SENT
    v.board_meeting_date = None
    v.save()
    v.responses.filter(extension_requested=True, handled=False).update(handled=True)
    log(v, f"Cure deadline extended {old:%b %d} → {new_deadline:%b %d, %Y}" + (f" ({reason})" if reason else ""), user)
    if notify:
        send_notice(v, Notice.Kind.EXTENSION, user)


def add_response(v, message, *, from_resident, user=None, extension_requested=False, extension_days=None):
    r = Response.objects.create(violation=v, message=message, from_resident=from_resident, author=user,
                                extension_requested=extension_requested, extension_days=extension_days)
    who = "Owner" if from_resident else "Staff"
    log(v, f"{who} " + ("requested an extension" if extension_requested else "replied"), user)
    return r


@transaction.atomic
def advance_expired(today=None):
    """Daily job: cure periods that ended without cure move to the board queue."""
    today = today or date.today()
    n = 0
    for v in Violation.objects.filter(status=S.NOTICE_SENT, cure_deadline__lt=today):
        v.status = S.PENDING_BOARD
        v.save()
        log(v, f"Cure period ended {v.cure_deadline:%b %d, %Y} without cure — referred to board")
        n += 1
    return n


@transaction.atomic
def board_decide(v, decision, meeting_date, user=None, note=""):
    """decision: 'proceed' | 'dismiss' | 'extend' (extend handled via grant_extension)."""
    _require(v, S.PENDING_BOARD)
    v.board_meeting_date = meeting_date
    if decision == "proceed":
        v.status = S.HEARING_NEEDED
        log(v, f"Board voted {meeting_date:%b %d, %Y} to proceed toward a fine" + (f": {note}" if note else ""), user)
    elif decision == "dismiss":
        v.status = S.DISMISSED
        log(v, f"Board voted {meeting_date:%b %d, %Y} to dismiss" + (f": {note}" if note else ""), user)
    else:
        raise WorkflowError("Unknown decision.")
    v.save()


@transaction.atomic
def schedule_hearing(v, hearing_dt, location="", user=None, today=None):
    _require(v, S.HEARING_NEEDED, S.HEARING_SCHEDULED)
    today = today or date.today()
    if isinstance(hearing_dt, datetime) and timezone.is_naive(hearing_dt):
        hearing_dt = timezone.make_aware(hearing_dt)
    min_day = today + timedelta(days=settings.HEARING_NOTICE_DAYS)
    if timezone.localtime(hearing_dt).date() < min_day:
        raise WorkflowError(f"Hearing must be at least {settings.HEARING_NOTICE_DAYS} days after notice "
                            f"(earliest {min_day:%b %d, %Y}).")
    v.hearing_date, v.hearing_location, v.status = hearing_dt, location, S.HEARING_SCHEDULED
    v.save()
    log(v, f"Committee hearing set for {timezone.localtime(hearing_dt):%b %d, %Y %I:%M %p}", user)
    return send_notice(v, Notice.Kind.HEARING, user)


def fine_warnings(v, amount):
    w = []
    amount = Decimal(amount)
    if amount > settings.FINE_MAX_PER_VIOLATION:
        w.append(f"Exceeds ${settings.FINE_MAX_PER_VIOLATION} per-violation limit (Fla. Stat. 720.305) "
                 "— allowed only if your governing documents provide otherwise.")
    if v.fines_total_for_owner_same_type() + amount > settings.FINE_AGGREGATE_CAP:
        w.append(f"Aggregate fines for this owner/violation type would exceed ${settings.FINE_AGGREGATE_CAP}.")
    return w


@transaction.atomic
def committee_decide(v, votes_for, votes_against, amount, user=None, note=""):
    """Committee (3+ owners who are not board members/relatives) must approve by majority to levy a fine."""
    _require(v, S.HEARING_SCHEDULED)
    if v.hearing_date and v.hearing_date > timezone.now():
        raise WorkflowError("The hearing date has not occurred yet.")
    if votes_for + votes_against < 3:
        raise WorkflowError("A fining committee must have at least 3 members voting.")
    v.committee_votes_for, v.committee_votes_against = votes_for, votes_against
    if votes_for > votes_against:
        v.status, v.fine_amount = S.FINE_APPROVED, Decimal(amount)
        log(v, f"Committee approved fine of ${Decimal(amount):,.2f} ({votes_for}-{votes_against})"
            + (f": {note}" if note else ""), user)
        v.save()
        return send_notice(v, Notice.Kind.FINE, user)
    v.status = S.FINE_REJECTED
    log(v, f"Committee rejected the fine ({votes_for}-{votes_against})" + (f": {note}" if note else ""), user)
    v.save()
