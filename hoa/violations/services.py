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

from .models import EmailLog, Event, Notice, Reminder, Response, Violation
from . import ledger, mailing
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


def recipient_groups(v):
    """[(who, emails, name)]: the owner(s) (every email on record) and, while the lease is active, the tenant(s).
    Each group gets its own email, addressed by name, with its own letter."""
    o = v.owner
    groups = []
    if o.all_emails:
        groups.append(("owner", o.all_emails, o.display_name))
    if o.has_tenant and o.tenant_emails:
        groups.append(("tenant", o.tenant_emails, o.tenant_name))
    return groups


def _signature():
    contact = ", ".join(x for x in (settings.HOA_CONTACT_EMAIL, settings.HOA_PHONE) if x)
    return (f"\n\nQuestions? {contact}" if contact else "") + f"\n\nOn behalf of the Board of Directors\n{settings.HOA_NAME}"


def correspondence(v, staff=False):
    """Everything sent to the owner (and tenant) about this case, newest last: each letter with how it was delivered, and each email."""
    items = []
    for n in v.notices.all():
        items.append({"when": n.created_at, "kind": "letter", "title": n.get_kind_display(), "notice": n,
                      "lines": n.delivery_lines(staff), "emails": [e for e in n.email_logs.all()]})
    for e in v.emails.filter(notice__isnull=True):
        items.append({"when": e.sent_at, "kind": "email", "title": e.subject, "email": e, "lines": []})
    items.sort(key=lambda i: i["when"])
    return items


def add_business_days(start, n):
    """The date n weekdays (Mon-Fri) after `start`. Holidays are not skipped."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def send_notice(v, kind, user=None, hold_mail_days=0):
    """Create a notice, email each recipient (owner, and tenant if rented) their own copy with the PDF attached, and print/mail it.
    With hold_mail_days > 0 the email goes now but the printed copy waits that many business days (see send_due_mailings).
    A failure to email or mail is recorded on the notice and never stops the case."""
    notice = Notice.objects.create(violation=v, kind=kind)
    if hold_mail_days:
        notice.mail_hold_until = add_business_days(date.today(), hold_mail_days)
        notice.save()
        log(v, f"{notice.get_kind_display()}: mailed copy held until {notice.mail_hold_until:%b %d, %Y} "
            f"({hold_mail_days} business days) in case it is corrected first")
    else:
        mailing.dispatch(notice, lambda who: render_notice_pdf(notice, who))
    sent_to, errors = [], []
    for who, emails, name in recipient_groups(v):
        try:
            pdf = render_notice_pdf(notice, who)
            subject = f"{notice.get_kind_display()} - {v.owner.property_address} (Case #{v.pk})"
            body = (f"Dear {name},\n\nPlease see the attached {notice.get_kind_display().lower()} regarding "
                    f"{v.owner.property_address}.\n\nYou can view details, reply, request more time or send photos of the cure here:\n"
                    f"{v.resident_url()}{_signature()}")
            msg = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, emails)
            msg.attach(f"case-{v.pk}-{kind}.pdf", pdf, "application/pdf")
            msg.send()
            sent_to += emails
            EmailLog.objects.create(violation=v, notice=notice, subject=subject, body=body, to=", ".join(emails), recipient=who)
        except Exception as e:  # SMTP down, bad address...
            errors.append(f"{who}: {e}"[:150])
            EmailLog.objects.create(violation=v, notice=notice, subject=subject, body=body, to=", ".join(emails), recipient=who,
                                    ok=False, error=str(e)[:300])
    if sent_to:
        notice.emailed_at, notice.emailed_to = timezone.now(), ", ".join(sent_to)
    notice.email_error = "; ".join(errors)[:300]
    notice.save()
    if sent_to:
        log(v, f"{notice.get_kind_display()} emailed to {notice.emailed_to}", user)
    if errors:
        log(v, f"{notice.get_kind_display()}: EMAIL FAILED ({notice.email_error})", user)
    if not sent_to and not errors:
        log(v, f"{notice.get_kind_display()} created (no email on file - mail only)", user)
    return notice


@transaction.atomic
def issue_first_notice(v, user=None, today=None, timing=None):
    """Standard: email now, mail after settings.MAIL_DELAY_BUSINESS_DAYS business days. Urgent: email and mail now.
    The cure period runs from the day the email goes out."""
    _require(v, S.DRAFT)
    timing = timing or v.issuance_timing or Violation.Timing.STANDARD
    if not v.photos.filter(from_resident=False).exists():
        raise WorkflowError("Add at least one photo of the violation before sending.")
    today = today or date.today()
    v.cure_deadline = today + timedelta(days=v.cure_days)
    v.status = S.NOTICE_SENT
    v.issuance_timing = timing
    v.save()
    log(v, f"Notice of Violation issued ({'urgent: email and mail now' if timing == Violation.Timing.URGENT else 'standard: email now, mail later'}); "
           f"cure deadline {v.cure_deadline:%b %d, %Y}", user)
    delay = 0 if timing == Violation.Timing.URGENT else settings.MAIL_DELAY_BUSINESS_DAYS
    return send_notice(v, Notice.Kind.VIOLATION, user, hold_mail_days=delay)


def _mail_due(notice):
    """Hand a held notice to the mail provider now (it stays on the print list when no provider is on)."""
    notice.mail_tried_on = date.today()
    notice.save()
    mailing.dispatch(notice, lambda who: render_notice_pdf(notice, who))


def cancel_held_mailings(v, reason="corrected before the mail date; postage saved"):
    n = 0
    for notice in v.notices.filter(mail_hold_until__isnull=False, mailed_on__isnull=True, mail_cancelled=False):
        notice.mail_cancelled = True
        notice.save()
        log(v, f"{notice.get_kind_display()}: mailing cancelled ({reason})")
        n += 1
    return n


def mail_now(notice, user=None):
    """Override the delay: send this held notice to the mail provider immediately."""
    if not notice.mail_held:
        raise WorkflowError("This notice is not waiting to be mailed.")
    log(notice.violation, f"{notice.get_kind_display()}: mailed copy released early", user)
    _mail_due(notice)


def send_due_mailings(today=None):
    """Daily job: hand over held notices whose wait is over, unless the case was corrected or closed meanwhile."""
    today = today or date.today()
    sent = 0
    qs = Notice.objects.filter(mail_hold_until__lte=today, mailed_on__isnull=True, mail_cancelled=False).exclude(mail_tried_on=today)
    for notice in qs.select_related("violation__owner"):
        v = notice.violation
        if v.cured_on or v.status in (S.CURED, S.CLOSED, S.DISMISSED):
            cancel_held_mailings(v)
            continue
        _mail_due(notice)
        sent += 1
    return sent


@transaction.atomic
def delete_photo(v, photo, user=None):
    """Remove a wrongly added photo. Only while the violation is still a draft (once the notice is out, the photo is part of it)."""
    _require(v, S.DRAFT)
    if photo.violation_id != v.pk or photo.from_resident:
        raise WorkflowError("That photo can't be removed.")
    photo.image.delete(save=False)
    photo.delete()
    log(v, "Photo removed before sending", user)


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
    cancel_held_mailings(v)
    if v.status in (S.FINE_APPROVED, S.LEDGER_PENDING):
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
    _require(v, *[st for st in Violation.OPEN if st not in (S.DRAFT, S.LEDGER_PENDING)])
    v.status, v.legal_referred_on = S.REFERRED_LEGAL, date.today()
    v.save()
    log(v, "Referred to the Association's attorney" + (f": {note}" if note else ""), user)


@transaction.atomic
def put_on_hold(v, reason, until, user=None):
    _require(v, *[st for st in Violation.OPEN if st not in (S.DRAFT, S.ON_HOLD)])
    if not reason.strip():
        raise WorkflowError("Give a reason for the hold.")
    if not until or until <= date.today():
        raise WorkflowError("Choose the date the hold lasts until (after today).")
    v.held_from, v.hold_reason, v.held_on, v.hold_until, v.status = v.status, reason.strip(), date.today(), until, S.ON_HOLD
    v.save()
    log(v, f"Put on hold until {until:%b %d, %Y}: {reason.strip()}", user)


RESTART_CURE_FROM = (S.NOTICE_SENT, S.PENDING_BOARD, S.HEARING_NEEDED)   # holds before a hearing is set: the cure period starts over


@transaction.atomic
def resume(v, user=None, auto=False, today=None):
    """Lift a hold (by hand, or automatically on its 'until' date). Cases held before a hearing go back to the cure period, which
    starts over from today; later stages go back where they were."""
    _require(v, S.ON_HOLD)
    today = today or date.today()
    back = v.held_from or S.NOTICE_SENT
    restart = back in RESTART_CURE_FROM
    if restart:
        v.status, v.extended_days, v.off_hold_on = S.NOTICE_SENT, 0, today
        v.cure_deadline = today + timedelta(days=v.cure_days)
    else:
        v.status = back
    v.held_from, v.hold_reason, v.held_on, v.hold_until = "", "", None, None
    v.save()
    log(v, ("Hold ended automatically" if auto else "Hold lifted") + (f"; off hold, cure period restarted, new deadline {v.cure_deadline:%b %d, %Y}" if restart
        else f" - back to '{v.get_status_display()}'"), user)
    if restart:
        when = "immediately" if v.cure_days == 0 else f"by {v.cure_deadline:%A, %B %d, %Y}"
        _email_owner(v, f"Hold ended: violation at {v.owner.property_address}",
                     f"The hold on the {v.vtype.name.lower()} violation at {v.owner.property_address} has ended. The cure period has started over "
                     f"and the violation must now be corrected {when}.\n\nRequired cure: {v.cure_required}\n\n"
                     "If you have already corrected it, please tell us (a photo helps) so it can be reinspected. If you need more time, "
                     "please ask before the deadline. After the deadline the matter may be referred to the Board of Directors.")


def lift_expired_holds(today=None):
    """Holds whose 'until' date has arrived lift themselves (the cure period then starts over)."""
    today = today or date.today()
    n = 0
    for v in Violation.objects.filter(status=S.ON_HOLD, hold_until__lte=today).select_related("owner", "vtype"):
        resume(v, auto=True, today=today)
        n += 1
    return n


def send_hold_reminders(today=None):
    """Daily job: warn the owner that a hold is about to end and that they need to take action to cure (REMINDER_DAYS before)."""
    today = today or date.today()
    sent = 0
    for v in Violation.objects.filter(status=S.ON_HOLD, hold_until__gt=today).select_related("owner", "vtype"):
        left = (v.hold_until - today).days
        due = [d for d in sorted(settings.REMINDER_DAYS) if left <= d]
        if not due:
            continue
        done = set(v.reminders.filter(deadline=v.hold_until).values_list("days_before", flat=True))
        todo = [d for d in due if d not in done]
        if not todo:
            continue
        for d in todo:
            Reminder.objects.get_or_create(violation=v, deadline=v.hold_until, days_before=d,
                                           defaults={"sent_at": timezone.now() if d == todo[0] else None})
        if not recipient_groups(v):
            continue
        when = "tomorrow" if left == 1 else f"in {left} days"
        _email_owner(v, f"Reminder: the hold on the violation at {v.owner.property_address} ends {when}",
                     f"The hold on the {v.vtype.name.lower()} violation at {v.owner.property_address} ends on {v.hold_until:%A, %B %d, %Y} ({when}). "
                     "When it ends, the cure period starts over"
                     + (" and the violation must be corrected immediately" if v.cure_days == 0 else f" and you will have {v.cure_days} days to correct it")
                     + f".\n\nRequired cure: {v.cure_required}\n\nPlease take action now so the violation is corrected before the hold ends. "
                     "If it is already corrected, tell us (a photo helps) so it can be reinspected. If you need more time, please contact us before the hold ends.")
        log(v, f"Hold-ending reminder emailed ({left} day{'s' if left != 1 else ''} left)")
        sent += 1
    return sent


@transaction.atomic
def mark_reinspect(v, user=None, note="", notify=True):
    """The owner says it is fixed: flag the case 'Re-inspect'. The case keeps aging in its stage and stays on the verify list
    (dashboard and Board queue) until someone verifies it or records that it is still not corrected."""
    _require(v, *[st for st in Violation.OPEN if st != S.DRAFT])
    if v.reinspect_on:
        raise WorkflowError("This case is already marked for re-inspection.")
    v.reinspect_on = date.today()
    v.save()
    v.responses.filter(cure_reported=True, handled=False).update(handled=True)
    log(v, "Marked for re-inspection (owner reports it is corrected)" + (f": {note}" if note else ""), user)
    if notify:
        msg = "Thank you. Your reply has been received. We will verify the correction at our next inspection."
        add_response(v, msg, from_resident=False, user=user)


@transaction.atomic
def reinspect_result(v, corrected, user=None, note=""):
    """Record the result of the re-inspection: verified corrected (closes the case) or still not corrected (the case carries on)."""
    if not v.reinspect_on:
        raise WorkflowError("This case is not marked for re-inspection.")
    if corrected:
        mark_cured(v, user, note or "verified at re-inspection")
        v.reinspect_on = None
        v.save()
        return
    v.reinspect_on = None
    v.save()
    log(v, "Re-inspected: still not corrected" + (f" ({note})" if note else ""), user)
    if note:
        add_response(v, f"We re-inspected and the violation has not been corrected yet. {note}", from_resident=False, user=user)


@transaction.atomic
def dismiss(v, user=None, note=""):
    _require(v, *Violation.OPEN)
    v.status = S.DISMISSED
    cancel_held_mailings(v, "case dismissed")
    v.save()
    log(v, "Dismissed (no violation / withdrawn)" + (f": {note}" if note else ""), user)


def _email_owner(v, subject, body):
    """Plain email to the owner(s) and tenant, each addressed by name. Returns the recipients."""
    to = []
    for who, emails, name in recipient_groups(v):
        full = f"Dear {name},\n\n{body}\n\nView your case or reply: {v.resident_url()}{_signature()}"
        try:
            EmailMessage(subject, full, settings.DEFAULT_FROM_EMAIL, emails).send()
            to += emails
            EmailLog.objects.create(violation=v, subject=subject, body=full, to=", ".join(emails), recipient=who)
        except Exception as e:
            log(v, f"Email '{subject[:40]}' failed for {who}: {e}"[:300])
            EmailLog.objects.create(violation=v, subject=subject, body=full, to=", ".join(emails), recipient=who, ok=False, error=str(e)[:300])
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
        if v.total_cure_days == 0:
            continue   # "immediately" notices have no countdown to remind about
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
        if v.responses.filter(cure_reported=True, handled=False).exists() or not recipient_groups(v):
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
    send_due_mailings(today)
    lift_expired_holds(today)
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
def board_decide(v, decision, meeting_date, user=None, note="", fine=None, suspension="", votes_for=None, votes_against=None,
                 suspension_days=None):
    """Board vote at a meeting. 'proceed' only PROPOSES a fine and/or suspension; nothing is owed until the committee approves.
    Per the Fining Resolution it takes a majority of the Board; the vote is recorded. A suspension names the Common Areas /
    facilities and a number of days."""
    _require(v, S.PENDING_BOARD)
    v.board_meeting_date = meeting_date
    if decision == "proceed":
        fine = Decimal(fine) if fine not in (None, "") else None
        suspension = (suspension or "").strip()
        if not fine and not suspension:
            raise WorkflowError("Enter the proposed fine amount and/or suspension the Board voted on.")
        if votes_for is None or votes_against is None or votes_for <= votes_against:
            raise WorkflowError("A fine or suspension needs a majority of the Board. Enter the votes for and against "
                                "(votes for must be more than votes against).")
        if suspension and not suspension_days:
            raise WorkflowError("A suspension needs a number of days (the Board sets a reasonable period).")
        if suspension_days and not suspension:
            raise WorkflowError("Say which Common Areas or facilities the suspension applies to.")
        if suspension_days and suspension_days > settings.SUSPENSION_MAX_DAYS:
            raise WorkflowError(f"The Declaration (section 8.1(A)(i)) limits a suspension for an infraction to {settings.SUSPENSION_MAX_DAYS} days. "
                                "It can be renewed for successive periods if the infraction is not corrected.")
        problems = fine_problems(v, fine) if fine else []
        if problems:
            raise WorkflowError(" ".join(problems))
        v.proposed_fine, v.proposed_suspension, v.status = fine, suspension, S.HEARING_NEEDED
        v.proposed_suspension_days = suspension_days or None
        v.board_votes_for, v.board_votes_against = votes_for, votes_against
        what = " and ".join(x for x in [f"fine ${fine:,.2f}" if fine else "",
                                        f"suspension of {suspension} for {suspension_days} days" if suspension else ""] if x)
        log(v, f"Board voted {meeting_date:%b %d, %Y} ({votes_for}-{votes_against}) to levy: {what}"
            + (f". {note}" if note else ""), user)
    elif decision == "dismiss":
        v.status = S.DISMISSED
        log(v, f"Board voted {meeting_date:%b %d, %Y} to dismiss" + (f": {note}" if note else ""), user)
    else:
        raise WorkflowError("Unknown decision.")
    v.save()


def short_notice(meeting, today=None):
    """True if the hearing is closer than the Resolution's recommended notice (20 days). The law's minimum is 14."""
    today = today or date.today()
    return timezone.localtime(meeting.starts_at).date() < today + timedelta(days=settings.HEARING_NOTICE_RECOMMENDED_DAYS)


def create_meeting(starts_at, zoom_link="", location="", user=None, zoom_meeting_id="", zoom_passcode="", zoom_dial_in=""):
    if isinstance(starts_at, datetime) and timezone.is_naive(starts_at):
        starts_at = timezone.make_aware(starts_at)
    if not (zoom_link or location):
        raise WorkflowError("Add a Zoom link and/or a location for the meeting.")
    return CommitteeMeeting.objects.create(starts_at=starts_at, zoom_link=zoom_link, location=location,
                                           zoom_meeting_id=zoom_meeting_id, zoom_passcode=zoom_passcode, zoom_dial_in=zoom_dial_in)


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
def committee_decide(v, votes_for, votes_against, user=None, note="", today=None, findings="", cure_by=None):
    """Committee (3+ owners, none board/officer/employee/relative) confirms or rejects the Board's proposal.
    Majority approval makes it final; the Board does not vote again."""
    _require(v, S.HEARING_SCHEDULED)
    if v.hearing_date and v.hearing_date > timezone.now():
        raise WorkflowError("The hearing date has not occurred yet.")
    if votes_for + votes_against < 3:
        raise WorkflowError("The Compliance Committee must have at least 3 members voting.")
    v.committee_votes_for, v.committee_votes_against = votes_for, votes_against
    v.committee_findings, v.committee_cure_by = (findings or note or "").strip(), cure_by
    if votes_for > votes_against:
        v.status = S.LEDGER_PENDING
        v.fine_amount, v.suspension = v.proposed_fine, v.proposed_suspension
        v.suspension_days = v.proposed_suspension_days
        v.save()
        log(v, f"Committee approved ({votes_for}-{votes_against})" + (f": {note}" if note else "")
            + ". Saved. Awaiting Board/manager approval to post to the ledger and send the imposition letter.", user)
        return None
    v.status = S.FINE_REJECTED
    v.save()
    log(v, f"Committee rejected the proposal ({votes_for}-{votes_against}) — nothing imposed"
        + (f": {note}" if note else "") + ". Notice of committee decision sent.", user)
    return send_notice(v, Notice.Kind.FINE, user)


def finalize_imposition(v, user=None, method="manual", reference="", today=None):
    """Board member / manager approves the Committee's decision: post the fine to the owner's ledger (Vantaca API, or record that
    it was posted by hand), then send the Notice of Imposition to the owner (and tenant). The fine is due FINE_PAYMENT_DAYS days
    after the letter date. The Committee's approval is what imposes the fine (no new Board vote); this step is the administrative
    review and posting. Safe to retry: a fine already posted is never posted twice."""
    _require(v, S.LEDGER_PENDING)
    today = today or date.today()
    if v.fine_amount and not v.ledger_posted_on:
        if method == "api":
            try:
                reference = ledger.post_fine(v, today)
            except ledger.LedgerError as e:
                raise WorkflowError(str(e))
            how = "posted to the Vantaca ledger"
        else:
            how = "recorded as posted in Vantaca by hand"
        v.ledger_posted_on, v.ledger_reference = today, (reference or "").strip()[:120]
        v.save(update_fields=["ledger_posted_on", "ledger_reference"])  # committed now, so a later hiccup can't cause a double post
        log(v, f"Fine of ${v.fine_amount:,.2f} {how}" + (f" (ref {v.ledger_reference})" if v.ledger_reference else ""), user)
    with transaction.atomic():
        v.status = S.FINE_APPROVED
        v.fine_due_on = today + timedelta(days=settings.FINE_PAYMENT_DAYS + settings.FINE_MAIL_BUFFER_DAYS) if v.fine_amount else None
        v.suspension_effective_on = today if v.suspension else None
        v.save()
        log(v, "Imposition approved" + (f"; fine due {v.fine_due_on:%b %d, %Y}" if v.fine_due_on else "") + ". Notice of committee decision (written findings) sent.", user)
    notice = send_notice(v, Notice.Kind.FINE, user)
    _close_if_done(v, user)
    v.save()
    return notice


def owner_history(v, today=None):
    """What else this owner/home has on record, for the staff view of a violation: how many times the same violation has come up,
    their other open violations, past ones, fines imposed, and whether this is a repeat within 3 months of a cure."""
    today = today or date.today()
    others = list(v.owner.violations.exclude(pk=v.pk).exclude(status=S.DRAFT).select_related("vtype").order_by("-created_at"))
    open_states = set(Violation.OPEN) - {S.DRAFT}
    same_type = [o for o in others if o.vtype_id == v.vtype_id]
    same_section = [o for o in others if v.vtype.section and o.vtype.section == v.vtype.section and o.vtype.source == v.vtype.source
                    and o.vtype_id != v.vtype_id]
    open_others = [o for o in others if o.status in open_states]
    past = [o for o in others if o.status not in open_states]
    imposed = [o for o in others if o.fine_amount and o.status in (S.FINE_APPROVED, S.CLOSED, S.REFERRED_LEGAL)]
    this_issued = (v.notices.filter(kind=Notice.Kind.VIOLATION).values_list("created_at", flat=True).first() or v.created_at).date()
    repeat_of = next((o for o in same_type if o.cured_on and 0 <= (this_issued - o.cured_on).days <= 90), None)
    return {
        "others": others, "same_type": same_type, "same_section": same_section, "open_others": open_others, "past": past,
        "total": len(others) + 1, "fines_total": sum(o.fine_amount for o in imposed),
        "last_12_months": sum(1 for o in others if o.created_at.date() >= today - timedelta(days=365)) + 1,
        "last_12_list": [o for o in others if o.created_at.date() >= today - timedelta(days=365)],
        "imposed": imposed,
        "occurrence": len(same_type) + 1,           # this is the 1st, 2nd, 3rd... time for this violation type
        "repeat_of": repeat_of,
    }
