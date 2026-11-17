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

from .models import Event, Notice, Reminder, Response, Violation
from . import mailing
from .models import CommitteeMeeting
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
    mailing.dispatch(notice, lambda who: render_notice_pdf(notice, who))
    recipients = [e for e in (v.owner.email, v.owner.tenant_email) if e]
    if recipients:
        pdf = render_notice_pdf(notice)
        subject = f"{notice.get_kind_display()} — {v.owner.property_address} (Case #{v.pk})"
        body = (f"Dear {v.owner.name},\n\nPlease see the attached {notice.get_kind_display().lower()} regarding "
                f"{v.owner.property_address}.\n\nYou can view details, reply, request more time or send photos "
                f"of the cure here:\n{v.resident_url()}\n\n"
                + (f"Questions? {', '.join(x for x in (settings.HOA_CONTACT_EMAIL, settings.HOA_PHONE) if x)}\n\n"
                   if (settings.HOA_CONTACT_EMAIL or settings.HOA_PHONE) else "")
                + f"On behalf of the Board of Directors\n{settings.HOA_NAME}")
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
    _require(v, *[st for st in Violation.OPEN if st != S.DRAFT])
    v.status, v.legal_referred_on = S.REFERRED_LEGAL, date.today()
    v.save()
    log(v, "Referred to the Association's attorney" + (f": {note}" if note else ""), user)


@transaction.atomic
def put_on_hold(v, reason, user=None):
    _require(v, *[st for st in Violation.OPEN if st not in (S.DRAFT, S.ON_HOLD)])
    if not reason.strip():
        raise WorkflowError("Give a reason for the hold.")
    v.held_from, v.hold_reason, v.held_on, v.status = v.status, reason.strip(), date.today(), S.ON_HOLD
    v.save()
    log(v, f"Put on hold: {reason.strip()}", user)


@transaction.atomic
def resume(v, user=None):
    _require(v, S.ON_HOLD)
    back = v.held_from or S.NOTICE_SENT
    if back == S.NOTICE_SENT and v.cure_deadline and v.held_on:  # the cure clock was paused
        v.cure_deadline += timedelta(days=(date.today() - v.held_on).days)
    v.status, v.held_from, v.hold_reason, v.held_on = back, "", "", None
    v.save()
    log(v, f"Hold lifted — back to '{v.get_status_display()}'", user)


@transaction.atomic
def dismiss(v, user=None, note=""):
    _require(v, *Violation.OPEN)
    v.status = S.DISMISSED
    v.save()
    log(v, "Dismissed (no violation / withdrawn)" + (f": {note}" if note else ""), user)


def _email_owner(v, subject, body):
    """Plain email to the owner (and tenant). Returns the recipients."""
    to = [e for e in (v.owner.email, v.owner.tenant_email) if e]
    if to:
        sign = (f"\n\n{', '.join(x for x in (settings.HOA_CONTACT_EMAIL, settings.HOA_PHONE) if x)}\n"
                if (settings.HOA_CONTACT_EMAIL or settings.HOA_PHONE) else "\n\n")
        EmailMessage(subject, f"Dear {v.owner.name},\n\n{body}\n\nView your case or reply: {v.resident_url()}"
                     f"{sign}On behalf of the Board of Directors\n{settings.HOA_NAME}",
                     settings.DEFAULT_FROM_EMAIL, to).send()
    return to


@transaction.atomic
def grant_extension(v, new_deadline=None, user=None, reason="", notify=True, second_notice=False, days=None, response=None):
    """Give more time. Either `days` (added to the CURRENT deadline, so the case ages as if the cure period had always
    been that long) or an exact `new_deadline`. As a 'second notice' it re-sends the full Notice of Violation."""
    _require(v, S.NOTICE_SENT, S.PENDING_BOARD)
    old = v.cure_deadline
    if days is not None:
        if days < 1:
            raise WorkflowError("Enter at least 1 day.")
        if not old:
            raise WorkflowError("This case has no cure deadline to extend.")
        new_deadline = old + timedelta(days=days)
    if new_deadline is None:
        raise WorkflowError("Enter the number of days to add, or an exact new deadline.")
    if new_deadline <= date.today():
        raise WorkflowError(f"The new deadline ({new_deadline:%b %d, %Y}) would already be in the past. "
                            "Enter a later exact date, or more days.")
    added = (new_deadline - old).days if old else 0
    v.extended_days += max(added, 0)
    v.cure_deadline, v.status = new_deadline, S.NOTICE_SENT
    v.board_meeting_date = None
    v.save()
    v.responses.filter(extension_requested=True, handled=False).update(handled=True)
    if response is not None:
        Response.objects.filter(pk=response.pk).update(handled=True, decision="approved")
    else:
        v.responses.filter(extension_requested=True, decision="", handled=True).update(decision="approved")
    log(v, f"Cure deadline {'reset' if second_notice else 'extended'}"
        + (f" by {added} days" if added > 0 else "") + f": {old:%b %d} → {new_deadline:%b %d, %Y}"
        + (f" ({reason})" if reason else ""), user)
    if notify:
        send_notice(v, Notice.Kind.SECOND if second_notice else Notice.Kind.EXTENSION, user)


@transaction.atomic
def decline_extension(v, response, reason="", user=None):
    """Say no to a request for more time. The owner gets the reason in the portal and by email."""
    _require(v, S.NOTICE_SENT, S.PENDING_BOARD)
    Response.objects.filter(pk=response.pk).update(handled=True, decision="declined")
    msg = (f"Your request for more time on the violation at {v.owner.property_address} was not approved. "
           + (f"Reason: {reason.strip()} " if reason.strip() else "")
           + (f"The cure deadline remains {v.cure_deadline:%B %d, %Y}." if v.status == S.NOTICE_SENT and v.cure_deadline
              else "The case continues through the Association's process."))
    Response.objects.create(violation=v, from_resident=False, author=user, message=msg)
    log(v, "Extension request declined" + (f": {reason.strip()}" if reason.strip() else ""), user)
    _email_owner(v, f"Your request for more time - {v.owner.property_address}", msg)


def send_cure_reminders(today=None):
    """Daily job: email owners as the cure deadline approaches (REMINDER_DAYS, default 7 and 2 days before).
    One reminder per threshold; if the job missed days, only the most urgent unsent one goes out. Not sent while a case
    is on hold, after the deadline, or when the owner has already said it's fixed and is awaiting reinspection."""
    today = today or date.today()
    sent = 0
    for v in Violation.objects.filter(status=S.NOTICE_SENT, cure_deadline__gte=today).select_related("owner", "vtype"):
        left = (v.cure_deadline - today).days
        due = [d for d in sorted(settings.REMINDER_DAYS) if left <= d]  # most urgent threshold first
        if not due:
            continue
        done = set(v.reminders.filter(deadline=v.cure_deadline).values_list("days_before", flat=True))
        todo = [d for d in due if d not in done]
        if not todo:
            continue
        mark = lambda sent_at: [Reminder.objects.get_or_create(violation=v, deadline=v.cure_deadline, days_before=d,
                                                                defaults={"sent_at": sent_at if d == todo[0] else None})
                                for d in todo]  # the rest are recorded as skipped so they never fire later
        if v.responses.filter(cure_reported=True, handled=False).exists() or not (v.owner.email or v.owner.tenant_email):
            mark(None)
            continue
        when = "today" if left == 0 else "tomorrow" if left == 1 else f"in {left} days"
        _email_owner(v, f"Reminder: violation at {v.owner.property_address} is due {when}",
                     f"This is a friendly reminder that the {v.vtype.name.lower()} violation at {v.owner.property_address} "
                     f"must be corrected by {v.cure_deadline:%A, %B %d, %Y} ({when}).\n\nRequired cure: {v.cure_required}\n\n"
                     "If you have already corrected it, please tell us (a photo helps) so it can be reinspected. If you need more "
                     "time, please ask before the deadline. After the deadline the matter may be referred to the Board of Directors.")
        mark(timezone.now())
        log(v, f"Reminder emailed to owner ({left} day{'s' if left != 1 else ''} left)")
        sent += 1
    return sent


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


def create_meeting(starts_at, zoom_link="", location="", user=None):
    if isinstance(starts_at, datetime) and timezone.is_naive(starts_at):
        starts_at = timezone.make_aware(starts_at)
    if not (zoom_link or location):
        raise WorkflowError("Add a Zoom link and/or a location for the meeting.")
    return CommitteeMeeting.objects.create(starts_at=starts_at, zoom_link=zoom_link, location=location)


@transaction.atomic
def assign_to_meeting(v, meeting, user=None, today=None):
    """Send a board-approved case to a Compliance Committee meeting: stage changes and the Notice of Hearing
    (with the Zoom link, proposed amount and the original notice) goes out by email and certified + regular mail."""
    _require(v, S.HEARING_NEEDED, S.HEARING_SCHEDULED)
    today = today or date.today()
    min_day = today + timedelta(days=settings.HEARING_NOTICE_DAYS)
    if timezone.localtime(meeting.starts_at).date() < min_day:
        raise WorkflowError(f"Hearing must be at least {settings.HEARING_NOTICE_DAYS} days after notice "
                            f"(earliest {min_day:%b %d, %Y}).")
    v.committee_meeting, v.hearing_date, v.hearing_location = meeting, meeting.starts_at, meeting.place
    v.status = S.HEARING_SCHEDULED
    v.save()
    log(v, f"Sent to Compliance Committee meeting {timezone.localtime(meeting.starts_at):%b %d, %Y %I:%M %p}", user)
    return send_notice(v, Notice.Kind.HEARING, user)


def assign_all_to_meeting(meeting, user=None, today=None):
    """Batch: every board-approved case waiting for a hearing goes onto this meeting. Returns (done, skipped)."""
    done, skipped = [], []
    for v in Violation.objects.filter(status=S.HEARING_NEEDED):
        try:
            assign_to_meeting(v, meeting, user, today)
            done.append(v)
        except WorkflowError as e:
            skipped.append((v, str(e)))
    return done, skipped


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
