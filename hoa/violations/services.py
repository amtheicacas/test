"""All workflow transitions live here so views, the CLI job and tests share one set of rules.

Follows the Heron Landing "Violation and Fining Procedure" (rev. Oct 2026) and flowchart:
  1 violation noted -> 2 Notice of Violation (usually 15 days) -> 3 owner fixes / justifies delay
    (optional 2nd notice with new deadline) -> 4 Board votes to PROPOSE a fine and/or suspension
  -> 5 Notice of Hearing (certified + regular mail + email, >= 14 days ahead, includes the NOV)
  -> 6 Compliance Committee hearing -> 7 majority approves (imposed, due in 5 days) or rejects
  -> 8 closed when cured and paid, else referred to the attorney.
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


def send_notice(v, kind, user=None):
    """Create a notice and email it with the PDF to the owner (and tenant, if any). Mailing is logged separately."""
    notice = Notice.objects.create(violation=v, kind=kind)
    recipients = [e for e in (v.owner.email, v.owner.tenant_email) if e]
    if recipients:
        pdf = render_notice_pdf(notice)
        subject = f"{notice.get_kind_display()} — {v.owner.property_address} (Case #{v.pk})"
        body = (f"Dear {v.owner.name},\n\nPlease see the attached {notice.get_kind_display().lower()} regarding "
                f"{v.owner.property_address}.\n\nYou can view details, reply, request more time or send photos "
                f"of the cure here:\n{v.resident_url()}\n\nQuestions? {settings.MANAGER_NAME}, "
                f"{settings.MANAGER_EMAIL}, {settings.OFFICE_PHONE}\n\n{settings.HOA_NAME}")
        msg = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, recipients)
        msg.attach(f"case-{v.pk}-{kind}.pdf", pdf, "application/pdf")
        msg.send()
        notice.emailed_at, notice.emailed_to = timezone.now(), ", ".join(recipients)
        notice.save()
        log(v, f"{notice.get_kind_display()} emailed to {notice.emailed_to}", user)
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
    log(v, f"Notice of Violation issued; cure deadline {v.cure_deadline:%b %d, %Y}", user)
    return send_notice(v, Notice.Kind.VIOLATION, user)


def mark_mailed(notice, mailed_on, tracking="", user=None, method="", return_receipt_on=None):
    notice.mailed_on, notice.tracking_number = mailed_on, tracking
    notice.method, notice.return_receipt_on = method, return_receipt_on
    notice.save()
    log(notice.violation, f"{notice.get_kind_display()} {notice.get_method_display().lower() or 'mailed'} "
        f"{mailed_on:%b %d, %Y}" + (f" (tracking {tracking})" if tracking else "")
        + (f"; return receipt {return_receipt_on:%b %d}" if return_receipt_on else ""), user)


def _close_if_done(v, user=None):
    """Case closes once the violation is corrected and any fine is paid."""
    if v.status == S.FINE_APPROVED and v.cured_on and (not v.fine_amount or v.fine_paid_on):
        v.status = S.CLOSED
        log(v, "Case closed — corrected and fine paid", user)


@transaction.atomic
def mark_cured(v, user=None, note="", today=None):
    _require(v, *Violation.OPEN)
    v.cured_on = today or date.today()
    if v.status == S.FINE_APPROVED:
        log(v, "Violation corrected" + (f": {note}" if note else ""), user)
        _close_if_done(v, user)
    else:
        v.status = S.CURED
        log(v, "Marked cured — case closed, no fine" + (f": {note}" if note else ""), user)
    v.responses.filter(cure_reported=True, handled=False).update(handled=True)
    v.save()


@transaction.atomic
def record_payment(v, paid_on, user=None):
    _require(v, S.FINE_APPROVED)
    if not v.fine_amount:
        raise WorkflowError("There is no fine to pay (suspension only).")
    v.fine_paid_on = paid_on
    log(v, f"Fine of ${v.fine_amount:,.2f} paid {paid_on:%b %d, %Y}", user)
    _close_if_done(v, user)
    v.save()


@transaction.atomic
def refer_to_attorney(v, user=None, note=""):
    _require(v, S.FINE_APPROVED)
    v.status, v.legal_referred_on = S.REFERRED_LEGAL, date.today()
    v.save()
    log(v, "Referred to the Association's attorney" + (f": {note}" if note else ""), user)


@transaction.atomic
def dismiss(v, user=None, note=""):
    _require(v, *Violation.OPEN)
    v.status = S.DISMISSED
    v.save()
    log(v, "Dismissed (no violation / withdrawn)" + (f": {note}" if note else ""), user)


@transaction.atomic
def grant_extension(v, new_deadline, user=None, reason="", notify=True, second_notice=False):
    """New deadline. As a 'second notice' it re-sends the full Notice of Violation (optional step in the process)."""
    _require(v, S.NOTICE_SENT, S.PENDING_BOARD)
    if new_deadline <= date.today():
        raise WorkflowError("New deadline must be in the future.")
    old = v.cure_deadline
    v.cure_deadline, v.status = new_deadline, S.NOTICE_SENT
    v.board_meeting_date = None
    v.save()
    v.responses.filter(extension_requested=True, handled=False).update(handled=True)
    log(v, f"Cure deadline {'reset' if second_notice else 'extended'} {old:%b %d} → {new_deadline:%b %d, %Y}"
        + (f" ({reason})" if reason else ""), user)
    if notify:
        send_notice(v, Notice.Kind.SECOND if second_notice else Notice.Kind.EXTENSION, user)


def add_response(v, message, *, from_resident, user=None, extension_requested=False, extension_days=None,
                 cure_reported=False, attorney_notice=False):
    r = Response.objects.create(violation=v, message=message, from_resident=from_resident, author=user,
                                extension_requested=extension_requested, extension_days=extension_days,
                                cure_reported=cure_reported, attorney_notice=attorney_notice)
    who = "Owner" if from_resident else "Staff"
    what = ("reported the violation fixed (needs reinspection)" if cure_reported
            else "requested an extension" if extension_requested else "replied")
    log(v, f"{who} {what}", user)
    if attorney_notice:
        late = v.hearing_date and (v.hearing_date - timezone.now()) < timedelta(days=settings.ATTORNEY_NOTICE_DAYS)
        log(v, "Owner will bring an attorney to the hearing"
            + (f" — NOTE: less than {settings.ATTORNEY_NOTICE_DAYS} days' notice" if late else ""), user)
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
def refer_urgent(v, user=None, note=""):
    """Serious safety situation: Board may act before the cure deadline (hearing rules still apply)."""
    _require(v, S.NOTICE_SENT)
    v.safety_issue, v.status = True, S.PENDING_BOARD
    v.save()
    log(v, "Referred to board early — safety situation" + (f": {note}" if note else ""), user)


def fine_problems(v, amount):
    amount = Decimal(amount or 0)
    problems = []
    if amount < 0:
        problems.append("Fine can't be negative.")
    total = v.fines_total_for_owner_same_type() + amount
    if total > settings.FINE_AGGREGATE_CAP:
        problems.append(f"Fine exceeds the ${settings.FINE_AGGREGATE_CAP:,} total cap (up to "
                        f"${settings.FINE_MAX_PER_DAY}/day for a continuing violation).")
    return problems


@transaction.atomic
def board_decide(v, decision, meeting_date, user=None, note="", fine=None, suspension=""):
    """Board vote at a meeting. 'proceed' only PROPOSES a fine and/or suspension; nothing is owed until the committee approves."""
    _require(v, S.PENDING_BOARD)
    v.board_meeting_date = meeting_date
    if decision == "proceed":
        fine = Decimal(fine) if fine not in (None, "") else None
        suspension = (suspension or "").strip()
        if not fine and not suspension:
            raise WorkflowError("Enter the proposed fine amount and/or suspension the Board voted on.")
        problems = fine_problems(v, fine)
        if problems:
            raise WorkflowError(" ".join(problems))
        v.proposed_fine, v.proposed_suspension, v.status = fine, suspension, S.HEARING_NEEDED
        what = " and ".join(x for x in [f"fine ${fine:,.2f}" if fine else "", f"suspension ({suspension})" if suspension else ""] if x)
        log(v, f"Board voted {meeting_date:%b %d, %Y} to propose: {what}" + (f". {note}" if note else ""), user)
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
    log(v, f"Compliance Committee hearing set for {timezone.localtime(hearing_dt):%b %d, %Y %I:%M %p}", user)
    return send_notice(v, Notice.Kind.HEARING, user)


@transaction.atomic
def committee_decide(v, votes_for, votes_against, user=None, note="", today=None):
    """Committee (3+ owners, none board/officer/employee/relative) confirms or rejects the Board's proposal.
    Majority approval makes it final; the Board does not vote again."""
    _require(v, S.HEARING_SCHEDULED)
    if v.hearing_date and v.hearing_date > timezone.now():
        raise WorkflowError("The hearing date has not occurred yet.")
    if votes_for + votes_against < 3:
        raise WorkflowError("The Compliance Committee must have at least 3 members voting.")
    v.committee_votes_for, v.committee_votes_against = votes_for, votes_against
    if votes_for > votes_against:
        today = today or date.today()
        v.status = S.FINE_APPROVED
        v.fine_amount, v.suspension = v.proposed_fine, v.proposed_suspension
        v.fine_due_on = today + timedelta(days=settings.FINE_PAYMENT_DAYS) if v.fine_amount else None
        v.save()
        log(v, f"Committee approved ({votes_for}-{votes_against})" + (f": {note}" if note else ""), user)
        return send_notice(v, Notice.Kind.FINE, user)
    v.status = S.FINE_REJECTED
    v.save()
    log(v, f"Committee rejected the proposal ({votes_for}-{votes_against}) — nothing imposed"
        + (f": {note}" if note else ""), user)
