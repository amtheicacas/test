"""All workflow transitions live here so views, the CLI job and tests share one set of rules.

Follows the Heron Landing "Violation and Fining Procedure" (rev. Oct 2026) and flowchart:
  1 violation noted -> 2 Notice of Violation (usually 15 days) -> 3 owner fixes / justifies delay
    (optional 2nd notice with new deadline) -> 4 Board votes to PROPOSE a fine and/or suspension
  -> 5 Notice of Hearing (certified + regular mail + email, >= 14 days ahead, includes the NOV)
  -> 6 Compliance Committee hearing -> 7 majority approves (imposed, due in 5 days) or rejects
  -> 8 closed when cured and paid, else referred to the attorney.
"""
import logging
import re
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import connection, connections, transaction
from django.db.models import Q
from django.utils import timezone

from .models import CaseAbstention, EmailLog, Event, MailPiece, Notice, Reminder, Response, Violation, short_date
from . import audit, ledger, mailing
from .models import CommitteeMeeting
from .pdf import render_notice_pdf

S = Violation.Status
logger = logging.getLogger(__name__)


class WorkflowError(Exception):
    pass


class DecisionLetterFailed(WorkflowError):
    """The Committee's decision was saved (the fine imposed and posted, or rejected) but its letter could not be created. The decision stands; the person is told."""


def log(v, text, user=None):
    Event.objects.create(violation=v, text=text, user=user)


def _require(v, *allowed):
    """The case must be in one of the `allowed` statuses RIGHT NOW. The check uses the database's current status, not the copy this
    request loaded earlier, and (inside a transaction, on PostgreSQL) locks the row until the change is saved. So a double click, or two
    people acting on the same case at once, cannot both succeed: the second one sees the new status and is refused."""
    if v.pk:
        qs = Violation.objects.filter(pk=v.pk)
        if connection.in_atomic_block:
            qs = qs.select_for_update()
        current = qs.values_list("status", flat=True).first()
        if current is not None:
            v.status = current
    if v.status not in allowed:
        raise WorkflowError(f"Not allowed while status is '{v.get_status_display()}'.")


def _fresh(obj, *fields):
    """Lock the row of a case (or any record) and re-read the named fields (call inside a transaction). A decision about a double click, or
    about two people acting at once, must use what is true NOW, not what this request loaded a moment ago."""
    row = type(obj).objects.select_for_update().filter(pk=obj.pk).values(*fields).first()
    for k, value in (row or {}).items():
        setattr(obj, k, value)


def lists_login_email(user, owner):
    """True when the home's owner record lists an email address that is this login's own (the main address, a ';' extra or the co-owner's, in any
    capitals). A person who owns a SECOND home here is on both records with the same address, but their login is attached to only one of them."""
    if user is None or owner is None:
        return False
    mine = {x.strip().lower() for x in (getattr(user, "email", ""), getattr(user, "username", "")) if x and "@" in x}
    return bool(mine and mine & {e.strip().lower() for e in owner.all_emails})


def own_household(user, v):
    """True when it is this person's OWN household's case: the home their login is attached to, or any home whose owner record lists their own email
    address (a second home). It follows the home, not the job title: a Board member or a manager who owns a home here is covered, a manager who
    owns none never is."""
    profile = getattr(user, "profile", None) if user is not None else None
    if profile is not None and profile.owner_id and profile.owner_id == v.owner_id:
        return True
    return lists_login_email(user, v.owner)


def _not_your_own(v, user, what):
    """Nobody decides their own household's case in their own favour. A decision that could end the case, shorten it, reduce it or put it off
    (dismiss, mark corrected, more time, hold, the committee's decision, lowering or waiving a fine) must be made by someone else. Moving the case
    to its next stage (send the notice, refer it, send it to a meeting, approve a fine the committee confirmed) is not a decision and stays open."""
    if own_household(user, v):
        raise WorkflowError(f"This is your own household's case, so you can't {what} yourself. Ask another Board member or the manager to do it.")


def _save_locked(v, *allowed):
    """Save the case's changes (including the new status already set on `v`) only if the case is STILL in one of the `allowed`
    statuses, checked under a row lock. Used where two people (or a double click) could otherwise write over each other."""
    new_status = v.status
    with transaction.atomic():
        _require(v, *allowed)          # re-reads the real status (and refuses if it moved on)
        v.status = new_status
        v.save()


def _warn_if_photo_unreadable(v, kind, user=None):
    """The letters that carry the first photo (Notice of Violation, Second Notice, Notice of Hearing) are built without it, silently, if the
    stored photo cannot be read (pdf.py is frozen, so the check lives here). Record a visible WARNING on the case instead of hiding it."""
    if kind not in (Notice.Kind.VIOLATION, Notice.Kind.SECOND, Notice.Kind.HEARING):
        return
    ph = v.photos.filter(from_resident=False).first()
    if ph is None:
        return
    try:
        from io import BytesIO
        from reportlab.platypus import Image
        with ph.image.open("rb") as fh:
            Image(BytesIO(fh.read()))        # the same read and parse the letter does
    except Exception as e:      # noqa: BLE001  (file missing from storage, storage unreachable, not a readable picture...)
        log(v, f"WARNING: the violation photo could not be read, so the {Notice.Kind(kind).label.lower()} was built WITHOUT its photo. "
               f"Check the photo on this case and tell the owner if the letter needs to be re-sent. ({type(e).__name__})"[:480], user)


def recipient_groups(v):
    """[(who, emails, name)]: the owner(s) (every email on record) and, while the lease is active, the tenant(s).
    Each group gets its own email, addressed by name, with its own letter."""
    o = v.owner
    groups = []
    if o.all_emails:
        groups.append(("owner", o.all_emails, o.display_name))
    if o.tenant_is_emailed:
        groups.append(("tenant", o.tenant_emails, o.tenant_name))
    return groups


def one_line(text, limit=200):
    """Text for an email subject (or any one-line header): control characters and line breaks become spaces and runs of spaces collapse. A line
    break in an owner's address (an import can bring one in) used to make EVERY email about that owner fail with "Header values can't contain
    newlines", so the owner was never emailed a notice."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text))).strip()[:limit]


def cut_to(text, limit):
    """`text` shortened with an ellipsis to fit a database column of `limit` characters. PostgreSQL (the live database) refuses text longer than its column; SQLite
    (your own PC and the tests) accepts it, which used to hide every case of this."""
    text = str(text or "")
    return text if len(text) <= limit else text[:max(limit - 1, 0)] + "\u2026"


def fit_addresses(addresses, limit):
    """The email addresses as one line, `a@x.com, b@y.org`, that fits a column of `limit` characters: whole addresses, then " (+N more)" for the rest.
    (Every address was still emailed; this is only the note kept on the notice and in the email log.)"""
    addresses = list(addresses)
    full = ", ".join(addresses)
    if len(full) <= limit:
        return full
    shown = []
    for i, address in enumerate(addresses):
        tail = f" (+{len(addresses) - i - 1} more)" if i < len(addresses) - 1 else ""
        if len(", ".join(shown + [address])) + len(tail) > limit:
            break
        shown.append(address)
    return cut_to(", ".join(shown) + f" (+{len(addresses) - len(shown)} more)", limit)


def _signature():
    contact = ", ".join(x for x in (settings.HOA_CONTACT_EMAIL, settings.HOA_PHONE) if x)
    return (f"\n\nQuestions? {contact}" if contact else "") + f"\n\nOn behalf of the Board of Directors\n{settings.HOA_NAME}"


def correspondence(v, staff=False):
    """Everything sent to the owner (and tenant) about this case, newest last: each letter with how it was delivered, and each email."""
    items = []
    for n in v.notices.all():
        items.append({"when": n.created_at, "kind": "letter", "title": n.get_kind_display(), "notice": n,
                      "lines": n.delivery_lines(staff), "emails": [e for e in n.email_logs.all()],
                      "pieces": [p for p in n.pieces.all() if (p.is_certified or p.tracking_number or p.not_sent) and (staff or p.is_mailed)]})
    for e in v.emails.filter(notice__isnull=True):
        items.append({"when": e.sent_at, "kind": "email", "title": e.subject, "email": e, "lines": []})
    items.sort(key=lambda i: i["when"])
    return items


def refresh_mail_tracking(force_for=None, hours=6, errors=None):
    """Pull the latest USPS tracking for certified pieces that haven't been delivered yet (daily job; also a button on the case).
    Returns how many pieces changed. A lookup that fails never stops the others and never raises; when a list is given each failure is added
    to it (the daily job prints them, instead of quietly reporting 'no updates'). Failures are also written to the server log."""
    from . import mailing
    qs = MailPiece.objects.filter(service="certified", delivered_on__isnull=True).exclude(provider_id="")
    if force_for is not None:
        qs = qs.filter(notice__violation=force_for)
    else:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=90))
        qs = qs.filter(checked_at__isnull=True) | qs.filter(checked_at__lt=timezone.now() - timedelta(hours=hours))
    return sum(1 for p in qs.select_related("notice__violation") if mailing.refresh_piece(p, errors))


def add_business_days(start, n):
    """The date n weekdays (Mon-Fri) after `start`. Holidays are not skipped."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _keep_copy(notice, pdf, who, channel, to, name, user, errors, tracking="", piece=None):
    """Save the exact PDF that just went out (emailed or mailed). A failure is recorded on the case, never hidden and never fatal."""
    from . import letters
    try:
        letters.save_sent_copy(notice, pdf, who, channel, to=to, name=name, user=user, tracking=tracking, piece=piece)
    except Exception as e:      # noqa: BLE001  (storage unreachable, disk full...)
        errors.append(f"saving the copy ({channel}): {e}"[:150])
        log(notice.violation, f"WARNING: the exact copy of the {notice.get_kind_display().lower()} ({channel}) could not be saved: {e}"[:480], user)


_PRIVATE_LINK = re.compile(r"(/r/|/accounts/link/)[A-Za-z0-9_\-]{16,}")


def stored_copy(body):
    """What is kept in the Email log: the email as sent, but with the private case link (a key to the owner's page) replaced, so a
    database leak or backup is not also a set of working links. (EMAILLOG_REDACT_LINKS=0 keeps them.) The saved letter PDFs are unchanged."""
    if not settings.EMAILLOG_REDACT_LINKS:
        return body
    return _PRIVATE_LINK.sub(r"\1[private link removed from this stored copy]", body)


def send_notice(v, kind, user=None, hold_mail_days=0):
    """Create a notice, email each recipient (owner, and tenant if rented) their own copy with the PDF attached, and print/mail it.
    With hold_mail_days > 0 the email goes now but the printed copy waits that many business days (see send_due_mailings).
    A failure to email or mail is recorded on the notice and never stops the case."""
    if kind == Notice.Kind.HEARING:
        from . import letters
        if letters.original_notice_copy(v) is None:
            log(v, "NOTE: no exact copy of the original Notice of Violation was on file (it was sent before copies were kept), so a REBUILT copy "
                   "is attached to this Notice of Hearing.", user)
    notice = Notice.objects.create(violation=v, kind=kind)
    _warn_if_photo_unreadable(v, kind, user)
    if hold_mail_days:
        notice.mail_hold_until = add_business_days(date.today(), hold_mail_days)
        notice.save()
        log(v, f"{notice.get_kind_display()}: mailed copy held until {notice.mail_hold_until:%b %d, %Y} "
            f"({hold_mail_days} business days) in case it is corrected first")
    else:
        _queue_for_mail(notice, user)
    sent_to, errors = [], []
    for who, emails, name in recipient_groups(v):
        subject = one_line(f"{notice.get_kind_display()} - {v.owner.property_address} (Case #{v.pk})")       # built BEFORE the try: a failure below is
        body = (f"Dear {name},\n\nPlease see the attached {notice.get_kind_display().lower()} regarding "          # recorded with THIS recipient's text
                f"{v.owner.property_address}.\n\nYou can view details, reply, request more time or send photos of the cure through the {settings.APP_NAME} here:\n"
                f"{v.resident_url()}{_signature()}")
        try:
            pdf = render_notice_pdf(notice, who)
            msg = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, emails)
            msg.attach(f"case-{v.pk}-{kind}.pdf", pdf, "application/pdf")
            msg.send()
            _keep_copy(notice, pdf, who, "email", ", ".join(emails), name, user, errors)
            sent_to += emails
            EmailLog.objects.create(violation=v, notice=notice, subject=subject, body=stored_copy(body), recipient=who,
                                    to=fit_addresses(emails, EmailLog._meta.get_field("to").max_length))
        except Exception as e:  # SMTP down, bad address...
            errors.append(f"{who}: {e}"[:150])
            EmailLog.objects.create(violation=v, notice=notice, subject=subject, body=stored_copy(body), recipient=who,
                                    to=fit_addresses(emails, EmailLog._meta.get_field("to").max_length), ok=False, error=str(e)[:300])
    if sent_to:
        notice.emailed_at, notice.emailed_to = timezone.now(), fit_addresses(sent_to, Notice._meta.get_field("emailed_to").max_length)
    notice.email_error = "; ".join(errors)[:300]
    notice.save(update_fields=["emailed_at", "emailed_to", "email_error"])      # only what the email part owns: the hand-over of the letters may be running
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
    if timing not in Violation.Timing.values:            # (it comes straight from the posted form: anything else would be saved as it is)
        raise WorkflowError("That is not a notice timing. Choose standard (email now, mail the printed copy later) or urgent (email and mail now).")
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
    notice = send_notice(v, Notice.Kind.VIOLATION, user, hold_mail_days=delay)
    if v.non_curable:   # point in time: nothing to cure, so it goes straight to the Board queue
        v.status = S.PENDING_BOARD
        v.save()
        log(v, "Point-in-time violation: sent straight to the Board queue (no cure period)", user)
    return notice


# ---- handing letters to the print service (Lob) ----
# A notice that must go by mail is either HELD (the first notice: printed copy waits a couple of business days in case it is corrected; the daily job
# releases it) or QUEUED (hearing notice, decision letter, extension, urgent notice: it goes right away). On the live site (MAIL_BACKGROUND) queuing is all
# the page does; a background thread hands the letters over just after the page has answered, the daily job picks up anything still queued, and the
# dashboard flags anything that has waited too long. There is NO transaction around the calls to the print service: each letter is recorded the moment
# it is accepted, and a CLAIM (Notice.mail_claimed_at, a lease) keeps two people, or a page and the daily job, from handing over the same notice at once.
# A hand-over that stopped half way (a restart, a crash) is resumed after the lease lapses, and no letter goes twice (mailing.dispatch skips letters on record).
def _claim_notice(pk, today=None, held_due=False, any_hold=False, retry_failed=False):
    """Take the claim on one notice: True only if it is still waiting and nobody (alive) has it. One UPDATE decides it, so two callers at the same
    moment can never both win. `held_due`: also a held copy whose wait is over (the daily job). `any_hold`: any held copy (Mail now).
    `retry_failed`: a notice with letters listed as NOT sent that nobody has handled (Mail now, to try them again)."""
    now, today = timezone.now(), today or date.today()
    waiting = Q(mailed_on__isnull=True) & Q(mail_queued_on__isnull=False)
    if any_hold:
        waiting |= Q(mailed_on__isnull=True) & Q(mail_hold_until__isnull=False)
    elif held_due:
        waiting |= Q(mailed_on__isnull=True) & Q(mail_hold_until__lte=today)
    if retry_failed:
        waiting |= Q(pk__in=MailPiece.objects.filter(by_hand=False, status=MailPiece.NOT_SENT, handled_on__isnull=True).values("notice_id"))
    lease_over = Q(mail_claimed_at__isnull=True) | Q(mail_claimed_at__lt=now - timedelta(minutes=settings.MAIL_LEASE_MINUTES))
    return bool(Notice.objects.filter(pk=pk, mail_cancelled=False).filter(waiting).filter(lease_over).update(mail_claimed_at=now, mail_tried_on=today))


def _do_hand_over(pk, user=None):
    """Hand over a notice whose claim this caller holds. A notice is DONE (taken off the queue, claim released) once any letter has gone or nothing
    is left to send; one where every letter failed stays queued, with the claim as its "last tried" time, and is tried again after the lease lapses
    (the next daily job). Letters that failed while others went are listed as NOT sent for a person (the red box); they are not retried by themselves."""
    notice = Notice.objects.select_related("violation__owner").get(pk=pk)
    who = user if getattr(user, "pk", None) else notice.mail_queued_by
    if notice.mail_hold_until and not notice.mail_queued_on:
        _warn_if_photo_unreadable(notice.violation, notice.kind, who)         # a held copy is built later than the notice was: look again
    mailing.dispatch(notice, lambda variant: render_notice_pdf(notice, variant), who)
    if notice.mailed_on or not mailing.letters_outstanding(notice):
        Notice.objects.filter(pk=pk).update(mail_queued_on=None, mail_claimed_at=None)


def _hand_over(pk, user=None, **claim):
    """Claim a notice and hand its letters over. False (and nothing done) if it is not waiting or someone else has it."""
    if not _claim_notice(pk, **claim):
        return False
    _do_hand_over(pk, user)
    return True


def hand_over_queued():
    """Hand over every QUEUED notice (held copies wait for the daily job). Each notice is claimed on its own; one that cannot be handed over is logged
    and stays queued, and never stops the others. Returns how many notices this call handled."""
    handled = 0
    for pk in list(Notice.objects.filter(mail_queued_on__isnull=False, mailed_on__isnull=True, mail_cancelled=False).values_list("pk", flat=True)):
        try:
            handled += bool(_hand_over(pk))
        except Exception:      # noqa: BLE001  (it stays queued and is tried again later)
            logger.exception("could not hand notice %s to the print service", pk)
    return handled


_kick_state = {"running": False, "again": False}
_kick_guard = threading.Lock()


def _start_thread(target):
    threading.Thread(target=target, name="hand-over-to-print-service", daemon=True).start()


def _background_hand_over():
    """The background thread: hand over what is queued, once more if more was queued while it worked, then stop. Never raises."""
    try:
        while True:
            try:
                hand_over_queued()
            except Exception:      # noqa: BLE001
                logger.exception("handing queued letters to the print service failed")
            with _kick_guard:
                if _kick_state["again"]:
                    _kick_state["again"] = False
                    continue
                _kick_state["running"] = False
                return
    finally:
        with _kick_guard:
            _kick_state["running"] = False
        if threading.current_thread() is not threading.main_thread():
            connections.close_all()         # a thread has its own database connections: close them when it ends


def _kick():
    """Ask for the queued letters to be handed over in the background (called once the page's own work has been saved). One thread per process
    however many times it is asked; if it is asked while it is working it makes one more pass. Anything it misses is picked up by the daily job."""
    try:
        with _kick_guard:
            if _kick_state["running"]:
                _kick_state["again"] = True
                return
            _kick_state["running"] = True
        _start_thread(_background_hand_over)
    except Exception:      # noqa: BLE001  (never let this break the page that asked)
        with _kick_guard:
            _kick_state["running"] = False
        logger.exception("could not start the hand-over to the print service")


def _queue_for_mail(notice, user=None):
    """Get the printed copies of a notice to the print service: queue them and, on the live site, ask for a background hand-over once the page's own
    work has been saved (the page does not wait for the print service); in development and in the tests hand them over at once. A newer notice of
    the same kind replaces one that is still waiting. With no print service the letters are listed for hand mailing at once, as before."""
    if settings.MAIL_PROVIDER != "lob":
        mailing.dispatch(notice, lambda who: render_notice_pdf(notice, who), user)
        return
    cancel_queued_mailings(notice.violation, "a newer notice of the same kind replaced it", kinds=(notice.kind,), exclude_pk=notice.pk)
    notice.mail_queued_on, notice.mail_queued_by = timezone.now(), (user if getattr(user, "pk", None) else None)
    notice.save(update_fields=["mail_queued_on", "mail_queued_by"])
    if settings.MAIL_BACKGROUND:
        transaction.on_commit(_kick)
    else:
        _hand_over(notice.pk, user)
        notice.refresh_from_db()


def cancel_held_mailings(v, reason="corrected before the mail date; postage saved"):
    n = 0
    for notice in v.notices.filter(mail_hold_until__isnull=False, mailed_on__isnull=True, mail_cancelled=False):
        if Notice.objects.filter(pk=notice.pk, mailed_on__isnull=True, mail_cancelled=False).update(mail_cancelled=True):      # only that one field
            log(v, f"{notice.get_kind_display()}: mailing cancelled ({reason})")
            n += 1
    return n


def cancel_queued_mailings(v, reason, kinds=None, exclude_pk=None):
    """Drop letters that are still waiting in the queue because they are no longer right (the hearing was moved or cancelled, or a newer notice of the
    same kind replaces them). A notice being handed over this very moment is left alone. Returns how many."""
    waiting = Notice.objects.filter(violation=v, mail_queued_on__isnull=False, mailed_on__isnull=True, mail_cancelled=False)
    if kinds:
        waiting = waiting.filter(kind__in=kinds)
    if exclude_pk:
        waiting = waiting.exclude(pk=exclude_pk)
    idle = Q(mail_claimed_at__isnull=True) | Q(mail_claimed_at__lt=timezone.now() - timedelta(minutes=settings.MAIL_LEASE_MINUTES))
    n = 0
    for notice in list(waiting):
        if Notice.objects.filter(pk=notice.pk, mailed_on__isnull=True, mail_cancelled=False).filter(idle).update(mail_cancelled=True, mail_queued_on=None):
            log(v, f"{notice.get_kind_display()}: mailing cancelled ({reason})")
            n += 1
    return n


def mail_now(notice, user=None):
    """Hand a waiting notice to the print service right now: a held copy before its date, letters still queued, or (to try again after the cause was
    fixed) the letters of a notice that were listed as NOT sent. A second click (or a second person) is refused: the notice is either being sent
    already or has nothing left to send."""
    n = Notice.objects.select_related("violation").filter(pk=notice.pk).first()
    retry = n is not None and MailPiece.objects.filter(notice=n, by_hand=False, status=MailPiece.NOT_SENT, handled_on__isnull=True).exists()
    if n is None or n.mail_cancelled or not (n.mail_waiting or retry):
        raise WorkflowError("This notice is not waiting to be mailed.")
    if not _claim_notice(n.pk, any_hold=True, retry_failed=True):
        raise WorkflowError("This notice is being sent right now (or was a moment ago). Give it a minute, then look at its mailing log.")
    log(n.violation, f"{n.get_kind_display()}: " + ("mailed copy released early" if n.mail_held else "letters sent again"), user)
    _do_hand_over(n.pk, user)


def send_due_mailings(today=None):
    """Daily job: hand over everything still waiting: held copies whose wait is over (unless the case was corrected or closed meanwhile: then the
    mailing is cancelled and the postage saved) and any letters still queued (a background hand-over that never ran or stopped half way).
    Each notice is claimed on its own; one that fails is logged and the others still go, then the job is told so it can say so."""
    today = today or date.today()
    sent, problems = 0, []
    due = Notice.objects.filter(Q(mail_queued_on__isnull=False) | Q(mail_hold_until__lte=today), mailed_on__isnull=True, mail_cancelled=False)
    for notice in list(due.select_related("violation")):
        v = notice.violation
        if not notice.mail_queued_on and (v.cured_on or v.status in (S.CURED, S.CLOSED, S.DISMISSED)):
            cancel_held_mailings(v)             # (a queued letter such as a decision letter still goes whatever has happened to the case since)
            continue
        try:
            sent += bool(_hand_over(notice.pk, today=today, held_due=True))
        except Exception as e:      # noqa: BLE001
            logger.exception("could not hand notice %s to the print service", notice.pk)
            problems.append(f"notice {notice.pk}: {type(e).__name__}")
    if problems:
        raise RuntimeError("These notices could not be handed to the print service (the others were): " + "; ".join(problems))
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
    notice.save(update_fields=["mailed_on", "tracking_number", "method", "return_receipt_on"])      # only what this records: never a stale copy of the rest
    log(notice.violation, f"{notice.get_kind_display()} {notice.get_method_display().lower() or 'mailed'} "
        f"{mailed_on:%b %d, %Y}" + (f" (tracking {tracking})" if tracking else "")
        + (f"; return receipt {return_receipt_on:%b %d}" if return_receipt_on else ""), user)


# ---- letters mailed by hand: one MailPiece per letter (certified / first class, per address) -----------------------------------------
def clean_tracking_number(raw):
    """What was typed or pasted from the Certified Mail receipt: spaces and dashes are dropped, letters made capital. Letters and numbers
    only, 8 to 40 of them (USPS certified numbers are 20 to 22 digits; the range is generous on purpose)."""
    t = re.sub(r"[\s\-]+", "", str(raw or "")).upper()
    if t and not re.fullmatch(r"[A-Z0-9]{8,40}", t):
        raise WorkflowError("The tracking number should be letters and numbers only (spaces and dashes are fine), 8 to 40 of them. "
                            "Copy it from the Certified Mail receipt.")
    return t


def _piece_log(piece, text, user=None):
    n = piece.notice
    log(n.violation, f"{n.get_kind_display()}: {text}", user)


def _piece_name(piece):
    return f"{piece.service_label.lower()} copy to {piece.label}"


def _sync_notice_from_pieces(n):
    """Keep the notice's own mailing fields in step with its letters (they drive the printed letter's tracking number, the owner's delivery
    lines, the progress tracker and the dashboard count): the certified tracking number and return receipt as soon as they are known,
    'mailed on' and the method once EVERY letter that is still needed has been mailed."""
    pieces = list(MailPiece.objects.filter(notice=n).order_by("pk"))
    live = [p for p in pieces if p.is_mailed or not p.handled_on]          # not closed as "not needed"
    if not live:
        return
    before = (n.mailed_on, n.method, n.tracking_number, n.return_receipt_on)
    cert = [p for p in live if p.is_certified]
    numbered = [p for p in cert if p.tracking_number]
    if numbered:
        n.tracking_number = numbered[0].tracking_number
    got = [p.delivered_on for p in cert if p.delivered_on]
    if got:
        n.return_receipt_on = min(got)
    mailed = [p for p in live if p.is_mailed]
    if len(mailed) == len(live):
        days = [p.mailed_on for p in (cert or live) if p.is_mailed and p.mailed_on]
        if days:
            n.mailed_on = min(days)
        n.method = "certified" if cert else "first_class"
    if before != (n.mailed_on, n.method, n.tracking_number, n.return_receipt_on):
        n.save(update_fields=["mailed_on", "method", "tracking_number", "return_receipt_on"])


def _check_day(day, label, today, not_before=None):
    if day is None:
        raise WorkflowError(f"Enter {label}.")
    if day > today:
        raise WorkflowError(f"{label[0].upper() + label[1:]} can't be in the future.")
    if not_before and day < not_before:
        raise WorkflowError(f"{label[0].upper() + label[1:]} can't be before it was mailed ({short_date(not_before)}).")


def _lock_piece(piece):
    """Lock the letter (and, first, its notice: the letters of one notice are recorded one at a time, so the notice's 'mailed on' and tracking number
    are always worked out from ALL of them) and re-read it. Call inside a transaction. Two people, or a double click, can then never overwrite
    each other, and every check that follows uses the letter as it is NOW."""
    notice_id = MailPiece.objects.filter(pk=piece.pk).values_list("notice_id", flat=True).first()
    Notice.objects.select_for_update().filter(pk=notice_id).first()
    MailPiece.objects.select_for_update().filter(pk=piece.pk).first()
    piece.refresh_from_db()


@transaction.atomic
def record_piece_mailed(piece, mailed_on, tracking="", user=None, today=None):
    """A letter was handed to the post office. A certified letter needs its tracking number (from the Certified Mail receipt).
    Also used to correct the date or number later."""
    _lock_piece(piece)
    today = today or date.today()
    _check_day(mailed_on, "the date it was mailed", today)
    tracking = clean_tracking_number(tracking)
    if piece.is_certified and not tracking:
        raise WorkflowError("Enter the certified mail tracking number (the long number on the Certified Mail receipt). "
                            "It is how we follow the letter and prove it was sent.")
    if piece.not_sent:
        raise WorkflowError("This letter never went out (the print service did not take it). Once it has been sent some other way, mark it handled "
                            "and say how.")
    if not piece.by_hand:
        raise WorkflowError("This letter was handed to the print service, which records the mailing itself.")
    again = piece.is_mailed
    piece.mailed_on = mailed_on
    if tracking or not piece.is_certified:
        piece.tracking_number = tracking
    if not piece.delivered_on and not piece.problem:
        piece.status = "Mailed"
    if piece.handled_on and not piece.problem:          # it had been closed as "not needed" but was mailed after all
        piece.handled_on, piece.handled_note = None, ""
    piece.save()
    _sync_notice_from_pieces(piece.notice)
    _piece_log(piece, f"{_piece_name(piece)} " + ("record corrected: " if again else "") + f"mailed {mailed_on:%b} {mailed_on.day}, {mailed_on.year}"
               + (f", tracking number {piece.tracking_number}" if piece.tracking_number else ""), user)
    return piece


@transaction.atomic
def record_piece_delivered(piece, delivered_on, user=None, today=None):
    """The post office delivered it (tracking says delivered, or the signed return receipt came back)."""
    _lock_piece(piece)
    today = today or date.today()
    if not piece.is_certified:
        raise WorkflowError("Only certified mail has a delivery record (first class mail has no tracking).")
    if not piece.is_mailed:
        raise WorkflowError("Record when it was mailed first.")
    _check_day(delivered_on, "the delivery date", today, not_before=piece.mailed_on)
    piece.delivered_on, piece.status, piece.problem = delivered_on, "Delivered", False
    piece.save()
    _sync_notice_from_pieces(piece.notice)
    _piece_log(piece, f"{_piece_name(piece)} delivered {delivered_on:%b} {delivered_on.day}, {delivered_on.year}"
               + (f" (tracking number {piece.tracking_number})" if piece.tracking_number else ""), user)
    return piece


@transaction.atomic
def record_piece_not_delivered(piece, reason, on=None, note="", user=None, today=None):
    """The letter could not be delivered (unclaimed, refused, returned...). It shows on the Board/manager home screen until someone
    records what was done about it ('handled')."""
    _lock_piece(piece)
    today = today or date.today()
    if not piece.is_certified:
        raise WorkflowError("Only certified mail has a delivery record (first class mail has no tracking).")
    if reason not in MailPiece.PROBLEMS:
        raise WorkflowError("Choose what happened to the letter.")
    if not piece.is_mailed:
        raise WorkflowError("This letter has not been mailed yet.")
    if piece.delivered_on:
        raise WorkflowError(f"This letter is recorded as delivered on {short_date(piece.delivered_on)}.")
    on = on or today
    _check_day(on, "the date you found out", today, not_before=piece.mailed_on)
    short, long = MailPiece.PROBLEMS[reason]
    piece.problem, piece.problem_on, piece.status = True, on, short
    piece.problem_note = (note or "").strip()[:300]
    piece.handled_on, piece.handled_note = None, ""      # a new result always reopens it
    piece.save()
    _piece_log(piece, f"WARNING: the {_piece_name(piece)} could not be delivered: {long}"
               + (f" ({piece.problem_note})" if piece.problem_note else "")
               + (f". Tracking number {piece.tracking_number}" if piece.tracking_number else "") + ".", user)
    return piece


@transaction.atomic
def record_piece_handled(piece, note, user=None, today=None):
    """Clear a delivery problem from the home screen once someone has dealt with it (the note says how), or mark a letter that was
    never mailed as 'not needed'. The record stays; only the warning goes away."""
    _lock_piece(piece)
    today = today or date.today()
    note = (note or "").strip()
    if len(note) < 3:
        raise WorkflowError("Say in a few words what was done (or why it is not needed): it is kept in the case history.")
    if (piece.is_mailed or piece.not_sent) and not piece.needs_attention:
        raise WorkflowError("There is nothing to clear on this letter.")
    unmailed = not piece.is_mailed
    piece.handled_on, piece.handled_note = today, note[:300]
    if unmailed and not piece.not_sent:
        piece.status = "Not mailed (not needed)"
    piece.save()
    _sync_notice_from_pieces(piece.notice)
    _piece_log(piece, (f"the {_piece_name(piece)} that was NOT sent was marked handled" if piece.not_sent
                       else f"the {_piece_name(piece)} was marked not needed" if unmailed
                       else f"delivery problem with the {_piece_name(piece)} marked handled") + f": {piece.handled_note}", user)
    return piece


def certified_to_check(today=None):
    """Certified letters mailed by hand CERTIFIED_CHECK_AFTER_DAYS or more ago with no result recorded yet (not delivered, no problem, not
    handled): the reminder to look at USPS tracking and record what happened. (Letters sent through the print service are tracked automatically.)"""
    days = settings.CERTIFIED_CHECK_AFTER_DAYS
    if days <= 0:
        return MailPiece.objects.none()
    today = today or date.today()
    return (MailPiece.objects.filter(by_hand=True, service="certified", mailed_on__lte=today - timedelta(days=days), delivered_on__isnull=True,
                                     problem=False, handled_on__isnull=True)
            .select_related("notice__violation__owner").order_by("mailed_on", "pk"))


def close_unmailed_pieces(v, why, user=None, exclude_notice=None, kinds=None):
    """The letters still waiting to be mailed by hand for this case are no longer needed (hearing moved or cancelled...). Returns how many."""
    qs = MailPiece.objects.filter(notice__violation=v, by_hand=True, mailed_on__isnull=True, handled_on__isnull=True)
    if exclude_notice is not None:
        qs = qs.exclude(notice=exclude_notice)
    if kinds:
        qs = qs.filter(notice__kind__in=kinds)
    pieces = list(qs.select_related("notice"))
    for p in pieces:
        p.handled_on, p.handled_note, p.status = date.today(), why[:300], "Not mailed (not needed)"
        p.save()
    for n in {p.notice_id: p.notice for p in pieces}.values():
        _sync_notice_from_pieces(n)
    if pieces:
        log(v, f"{len(pieces)} letter(s) that had not been mailed yet are no longer needed: {why}", user)
    return len(pieces)


def _close_if_done(v, user=None):
    """Case closes once the violation is corrected and any fine is paid."""
    if v.status == S.FINE_APPROVED and v.cured_on and (not v.fine_amount or v.fine_paid_on):
        v.status = S.CLOSED
        log(v, "Case closed — corrected and fine paid", user)


def posted_fine_warning(v):
    """"" or one plain sentence for a case whose fine is ALREADY POSTED to the owner's Vantaca ledger: closing the case here (dismissed, or corrected
    with no fine) does not take that charge off the ledger, and does not give back a payment. That has to be done in Vantaca."""
    if not (v.ledger_posted_on and v.fine_amount):
        return ""
    ref = f", reference {v.ledger_reference}" if v.ledger_reference else ""
    return (f"A fine of ${v.fine_amount:,.2f} was posted to this owner's ledger in Vantaca on {short_date(v.ledger_posted_on)}{ref}. "
            "Closing the case here does not take that charge off the ledger" + (" or give the payment back" if v.fine_paid_on else "")
            + ": that has to be done in Vantaca.")


def posted_fine_reminder(v):
    """The same fact as posted_fine_warning, said once the case HAS been closed."""
    if not posted_fine_warning(v):
        return ""
    return (f"The case is closed, but the ${v.fine_amount:,.2f} fine posted to this owner's ledger on {short_date(v.ledger_posted_on)} is still on the "
            "owner's ledger until it is reversed in Vantaca.")


def _closing_warning(v, note):
    """The warning for a case about to be closed WITHOUT its fine, refusing (with that warning) if no note says why."""
    warning = posted_fine_warning(v)
    if warning and not (note or "").strip():
        raise WorkflowError(warning + " Write a note saying why you are closing it (it is kept in the case history), and check the ledger first.")
    return warning


@transaction.atomic
def mark_cured(v, user=None, note="", today=None):
    _not_your_own(v, user, "mark it corrected")
    _require(v, *Violation.OPEN)
    imposed = v.status in (S.FINE_APPROVED, S.LEDGER_PENDING)      # the fine stands: the case stays open until it is paid
    warning = "" if imposed else _closing_warning(v, note)
    v.cured_on = today or date.today()
    cancel_held_mailings(v)
    if imposed:
        log(v, "Violation corrected" + (f": {note}" if note else ""), user)
        _close_if_done(v, user)
    else:
        v.status = S.CURED
        log(v, "Marked cured — case closed, no fine" + (f": {note}" if note else ""), user)
        if warning:
            log(v, "WARNING: " + warning, user)
    v.responses.filter(cure_reported=True, handled=False).update(handled=True)
    v.save()


@transaction.atomic
def record_payment(v, paid_on, user=None):
    _require(v, S.FINE_APPROVED)
    if not v.fine_amount:
        raise WorkflowError("There is no fine to pay" + (" (suspension only)." if settings.SUSPENSION_ENABLED else "."))
    v.fine_paid_on = paid_on
    log(v, f"Fine of ${v.fine_amount:,.2f} paid {paid_on:%b %d, %Y}", user)
    _close_if_done(v, user)
    v.save()


@transaction.atomic
def refer_to_attorney(v, user=None, note=""):
    """Mark the case as referred to the attorney. A fine imposed but the violation still not corrected is the normal path ('after_fine');
    anything else is a direct referral because of the nature of the violation ('direct'), which bypasses the Board / Committee steps.
    This sends nothing: the email to the attorney is drafted for review (legal_email_draft) and only sent when someone clicks Send."""
    _require(v, *[st for st in Violation.OPEN if st not in (S.DRAFT, S.LEDGER_PENDING, S.REFERRED_LEGAL)])
    v.legal_path = "after_fine" if v.status == S.FINE_APPROVED else "direct"
    v.status, v.legal_referred_on, v.legal_reason, v.legal_sent_on, v.legal_sent_to = S.REFERRED_LEGAL, date.today(), (note or "").strip(), None, ""
    v.save()
    cancel_held_mailings(v, "case referred to the attorney")
    log(v, ("Referred to the Association's attorney after the fine was imposed" if v.legal_path == "after_fine"
            else "Referred directly to the Association's attorney (Board / Committee steps bypassed)") + (f": {note}" if note else "")
        + ". Draft email to the attorney prepared, not sent.", user)


# ---- the email to the attorney: drafted for review, sent only when a person clicks Send ----
def _money(x):
    return f"${Decimal(x):,.2f}"


def legal_attachments(v):
    """[{key, label, filename, checked}]: every letter sent to the owner, the photos we sent them, and (unchecked) the owner's own photos."""
    import os
    out = []
    for n in v.notices.all():
        d = timezone.localtime(n.created_at)
        out.append({"key": f"letter-{n.pk}", "label": f"{n.get_kind_display()} ({d:%b} {d.day}, {d.year})",
                    "filename": f"{n.kind}-letter-{d:%Y-%m-%d}.pdf", "checked": True})
    for p in v.photos.all():
        mine = p.from_resident
        out.append({"key": f"photo-{p.pk}", "label": ("Owner's photo: " if mine else "Violation photo: ") + os.path.basename(p.image.name),
                    "filename": os.path.basename(p.image.name), "checked": not mine})
    return out


def legal_attachment_bytes(v, key):
    import os
    kind, _, pk = key.partition("-")
    if not (pk.isascii() and pk.isdigit() and len(pk) <= 18):      # a key that is not one of ours (edited by hand): simply no attachment
        return None
    if kind == "letter":
        n = v.notices.filter(pk=pk).first()
        return (f"{n.kind}-letter-{timezone.localtime(n.created_at):%Y-%m-%d}.pdf", render_notice_pdf(n, "owner"), "application/pdf") if n else None
    if kind == "photo":
        p = v.photos.filter(pk=pk).first()
        if not p:
            return None
        import mimetypes
        with p.image.open("rb") as f:
            return (os.path.basename(p.image.name), f.read(), mimetypes.guess_type(p.image.name)[0] or "application/octet-stream")
    return None


def legal_email_draft(v, user=None):
    """The drafted email (to, subject, body) summarizing the case for the attorney. Nothing is sent."""
    o = v.owner
    d = lambda x: f"{x:%B} {x.day}, {x.year}"
    who = (settings.ATTORNEY_NAME or "").strip()
    rule = " ".join(x for x in (("Declaration" if v.vtype.source == "Declaration" else "Community Standards") + (f" \u00a7{v.vtype.section}" if v.vtype.section else ""),
                                v.vtype.standards_title) if x)
    lines = [f"Dear {who or 'Counsel'},", "",
             "The Association is referring the following matter to you for review and further action.", "",
             "OWNER AND PROPERTY",
             f"Owner: {o.display_name}", f"Property: {o.property_address}" + (f" (Lot {o.lot_no})" if o.lot_no else "")]
    if o.has_separate_mailing:
        lines.append(f"Owner's mailing address: {o.mailing_address.strip()}")
    if o.has_tenant:
        lines.append(f"Tenant: {o.tenant_name}" + (f" (lease through {d(o.tenant_end)})" if o.tenant_end else ""))
    lines += ["", "VIOLATION", f"{v.vtype.name} ({rule})", f"Observed {d(v.observed_on)}: {v.description}", f"Required cure: {v.cure_required}", ""]
    tl = []
    for n in v.notices.all():
        dd = timezone.localtime(n.created_at)
        how = "first class mail and email" if n.kind in ("violation", "second", "extension") else "certified mail and email"
        tl.append((n.created_at, f"{d(dd)}: {n.get_kind_display()} sent by {how}" + (f" (certified tracking {n.tracking_number})" if n.tracking_number else "")))
    if v.cure_deadline:
        tl.append((timezone.make_aware(datetime.combine(v.cure_deadline, datetime.min.time())), f"{d(v.cure_deadline)}: cure deadline (including any extension)"))
    if v.board_meeting_date:
        tl.append((timezone.make_aware(datetime.combine(v.board_meeting_date, datetime.min.time())),
                   f"{d(v.board_meeting_date)}: Board voted to proceed" + (f" ({v.board_votes_for}-{v.board_votes_against})" if v.board_votes_for is not None else "")))
    if v.hearing_date:
        tl.append((v.hearing_date, f"{d(timezone.localtime(v.hearing_date))}: Fining Committee hearing" + (
            f"; decision by vote of {v.committee_votes_for}-{v.committee_votes_against}" if v.committee_votes_for is not None else "")))
    tl.sort(key=lambda x: x[0])
    if tl:
        lines += ["TIMELINE"] + [f"- {t}" for _, t in tl] + [""]
    lines.append("STATUS")
    if v.legal_path == "after_fine":
        if settings.SUSPENSION_ENABLED:
            amt = (f"a fine of {_money(v.fine_amount)}" if v.fine_amount else "a suspension of privileges") + (
                f" and a suspension of {v.suspension}" if v.fine_amount and v.suspension else "")
        else:
            amt = f"a fine of {_money(v.fine_amount)}" if v.fine_amount else "a fine"
        due = f", due {d(v.fine_due_on)}" if v.fine_due_on else ""
        paid = (" The fine has been paid." if v.fine_paid_on else (" The fine has not been paid." if v.fine_amount else ""))
        lines.append(f"This matter was issued {amt}{due}, following the Association's notice and hearing process, but the violation is not complete: it has not been corrected.{paid}")
    else:
        flags = ", ".join(x for x, on in (("non-curable", v.non_curable), ("safety issue", v.safety_issue), ("Board-declared emergency", v.board_emergency)) if on)
        lines.append("Because of the nature of this violation" + (f" ({flags})" if flags else "") + ", the Association is referring it directly to you without completing "
                     "the fining process (no fine has been imposed at this time).")
        if v.legal_reason:
            lines.append(f"Reason for the direct referral: {v.legal_reason}")
    replies = [r for r in v.responses.all() if r.from_resident][-3:]
    if replies:
        lines += ["", "OWNER'S RESPONSES"] + [f"- {timezone.localtime(r.created_at):%b} {timezone.localtime(r.created_at).day}: {r.message.strip()[:400]}" for r in replies]
    lines += ["", "Attached are the letters the Association sent to the owner and the photographs of the violation. Please let us know what additional information you need and how you recommend we proceed.", "",
              "Thank you,", (user.get_full_name() or user.get_username()) if user is not None and getattr(user, "is_authenticated", False) else "",
              "On behalf of the Board of Directors", settings.HOA_NAME]
    return {"to": settings.ATTORNEY_EMAIL, "subject": f"Referral - {v.vtype.name} - {o.property_address} (Case #{v.pk})",
            "body": "\n".join(lines), "attachments": legal_attachments(v)}


def send_legal_email(v, to, subject, body, keys, user=None, cc=""):
    """Send the reviewed email to the attorney with the chosen attachments. Only ever called when a person clicks Send."""
    _require(v, S.REFERRED_LEGAL)
    if "@" not in (to or ""):
        raise WorkflowError("Enter the attorney's email address.")
    msg = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [to.strip()], cc=[cc.strip()] if cc.strip() else [],
                       reply_to=[user.email] if user is not None and getattr(user, "email", "") else None)
    n = 0
    for k in keys:
        att = legal_attachment_bytes(v, k)
        if att:
            msg.attach(*att)
            n += 1
    try:
        msg.send()
    except Exception as e:
        raise WorkflowError(f"The email could not be sent: {e}")
    v.legal_sent_on, v.legal_sent_to = timezone.now(), to.strip()
    v.save(update_fields=["legal_sent_on", "legal_sent_to"])
    log(v, f"Email sent to the attorney ({to.strip()}) with {n} attachment(s)", user)


@transaction.atomic
def put_on_hold(v, reason, until, user=None):
    _not_your_own(v, user, "put it on hold")
    _require(v, *[st for st in Violation.OPEN if st not in (S.DRAFT, S.ON_HOLD, S.REFERRED_LEGAL)])
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
        try:
            resume(v, auto=True, today=today)
        except WorkflowError:
            continue            # someone lifted this hold (or changed the case) after the list was read: nothing left to do
        n += 1
    return n


def _record_reminders(v, deadline, todo, sent_at):
    """Record the reminder thresholds in `todo` as dealt with: the most urgent one with the time it was emailed (None = skipped), the rest as
    skipped so they never fire later."""
    for d in todo:
        Reminder.objects.get_or_create(violation=v, deadline=deadline, days_before=d, defaults={"sent_at": sent_at if d == todo[0] else None})


def send_hold_reminders(today=None):
    """Daily job: warn the owner that a hold is about to end and that they need to take action to cure (REMINDER_DAYS before).
    A reminder is recorded as sent only when at least one email really went out; if every email failed, nothing is recorded and the next
    daily job tries again."""
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
        if not recipient_groups(v):
            _record_reminders(v, v.hold_until, todo, None)         # nobody to email: skipped, not "sent"
            continue
        when = "tomorrow" if left == 1 else f"in {left} days"
        emailed = _email_owner(v, f"Reminder: the hold on the violation at {v.owner.property_address} ends {when}",
                               f"The hold on the {v.vtype.name.lower()} violation at {v.owner.property_address} ends on {v.hold_until:%A, %B %d, %Y} ({when}). "
                               "When it ends, the cure period starts over"
                               + (" and the violation must be corrected immediately" if v.cure_days == 0 else f" and you will have {v.cure_days} days to correct it")
                               + f".\n\nRequired cure: {v.cure_required}\n\nPlease take action now so the violation is corrected before the hold ends. "
                               "If it is already corrected, tell us (a photo helps) so it can be reinspected. If you need more time, please contact us before the hold ends.")
        if not emailed:
            continue                  # every email failed (each failure is on the case): not recorded, so the next daily job tries again
        _record_reminders(v, v.hold_until, todo, timezone.now())
        log(v, f"Hold-ending reminder emailed ({left} day{'s' if left != 1 else ''} left)")
        sent += 1
    return sent


@transaction.atomic
def mark_reinspect(v, user=None, note="", notify=True):
    """The owner says it is fixed: flag the case 'Re-inspect'. The case keeps aging in its stage and stays on the verify list
    (dashboard and Board queue) until someone verifies it or records that it is still not corrected."""
    _require(v, *[st for st in Violation.OPEN if st != S.DRAFT])
    _fresh(v, "reinspect_on")
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
    _not_your_own(v, user, "record the result of the re-inspection")
    _fresh(v, "reinspect_on")
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
    _not_your_own(v, user, "dismiss it")
    _require(v, *Violation.OPEN)
    warning = _closing_warning(v, note)
    v.status = S.DISMISSED
    cancel_held_mailings(v, "case dismissed")
    v.save()
    log(v, "Dismissed (no violation / withdrawn)" + (f": {note}" if note else ""), user)
    if warning:
        log(v, "WARNING: " + warning, user)


def _email_owner(v, subject, body):
    """Plain email to the owner(s) and tenant, each addressed by name. Returns the recipients."""
    to = []
    subject = one_line(subject)
    for who, emails, name in recipient_groups(v):
        full = f"Dear {name},\n\n{body}\n\nView your case or reply: {v.resident_url()}{_signature()}"
        try:
            EmailMessage(subject, full, settings.DEFAULT_FROM_EMAIL, emails).send()
            to += emails
            EmailLog.objects.create(violation=v, subject=subject, body=stored_copy(full), recipient=who,
                                    to=fit_addresses(emails, EmailLog._meta.get_field("to").max_length))
        except Exception as e:
            log(v, f"Email '{subject[:40]}' failed for {who}: {e}"[:300])
            EmailLog.objects.create(violation=v, subject=subject, body=stored_copy(full), recipient=who, ok=False, error=str(e)[:300],
                                    to=fit_addresses(emails, EmailLog._meta.get_field("to").max_length))
    return to


@transaction.atomic
def grant_extension(v, new_deadline=None, user=None, reason="", notify=True, second_notice=False, days=None, response=None,
                    deadline_was=None, from_status=None):
    """Give more time. Either `days` (added to the CURRENT deadline, so the case ages as if the cure period had always
    been that long) or an exact `new_deadline`. As a 'second notice' it re-sends the full Notice of Violation.
    A second press of the same button must not add the days (or send the letter) again, so the case is checked under its lock: `deadline_was` is the
    deadline the page showed when it was opened ('YYYY-MM-DD', or '' for none; None = the caller does not know) and must still be the deadline;
    `from_status` limits the stages it may be used from (the Board's vote uses only 'awaiting the Board'); and a request for more time that is
    being answered must still be unanswered."""
    _not_your_own(v, user, "give it more time")
    _require(v, *(from_status or (S.NOTICE_SENT, S.PENDING_BOARD)))
    _fresh(v, "cure_deadline", "extended_days")          # the deadline as it is NOW, not as this request loaded it
    if deadline_was is not None and (v.cure_deadline.isoformat() if v.cure_deadline else "") != str(deadline_was).strip():
        raise WorkflowError(f"The deadline was changed a moment ago (it is now {short_date(v.cure_deadline) if v.cure_deadline else 'not set'}, "
                            "not what the page you opened showed), so nothing more was added. Open the case again and check it; "
                            "if you still want to give more time, do it from there.")
    if response is not None and not Response.objects.select_for_update().filter(pk=response.pk, handled=False).exists():
        raise WorkflowError("That request for more time has already been answered.")
    old = v.cure_deadline
    if days is not None:
        if days < 1:
            raise WorkflowError("Enter at least 1 day.")
        if not old:
            raise WorkflowError("This case has no cure deadline to extend.")
        new_deadline = old + timedelta(days=days)
    if new_deadline is None:
        raise WorkflowError("Enter the number of days to add, or an exact new deadline.")
    if days is None and new_deadline > date.today() + timedelta(days=365):           # a typed date: a typo such as 2062 must not park a case for decades
        raise WorkflowError(f"The new deadline ({new_deadline:%b %d, %Y}) is more than a year away. Check the date. "
                            "If you really mean a longer time, give a year now and extend it again later.")
    if new_deadline <= date.today():
        raise WorkflowError(f"The new deadline ({new_deadline:%b %d, %Y}) would already be in the past. "
                            "Enter a later exact date, or more days.")
    if old and new_deadline <= old and not second_notice:      # "more time" must never shorten the cure period (a second notice may reset it to any future date)
        raise WorkflowError(f"The new deadline ({new_deadline:%b %d, %Y}) is not later than the current deadline ({old:%b %d, %Y}). "
                            "To give more time, enter a later exact date, or a number of days to add.")
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
    _not_your_own(v, user, "answer a request for more time")
    _require(v, S.NOTICE_SENT, S.PENDING_BOARD)
    if not Response.objects.select_for_update().filter(pk=response.pk, handled=False).exists():       # (two people answering at once: the second is refused)
        raise WorkflowError("That request for more time has already been answered.")
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
        if v.responses.filter(cure_reported=True, handled=False).exists() or not recipient_groups(v):
            _record_reminders(v, v.cure_deadline, todo, None)           # skipped, not "sent"
            continue
        when = "today" if left == 0 else "tomorrow" if left == 1 else f"in {left} days"
        emailed = _email_owner(v, f"Reminder: violation at {v.owner.property_address} is due {when}",
                               f"This is a friendly reminder that the {v.vtype.name.lower()} violation at {v.owner.property_address} "
                               f"must be corrected by {v.cure_deadline:%A, %B %d, %Y} ({when}).\n\nRequired cure: {v.cure_required}\n\n"
                               "If you have already corrected it, please tell us (a photo helps) so it can be reinspected. If you need more "
                               "time, please ask before the deadline. After the deadline the matter may be referred to the Board of Directors.")
        if not emailed:
            continue                  # every email failed (each failure is on the case): not recorded, so the next daily job tries again
        _record_reminders(v, v.cure_deadline, todo, timezone.now())
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


def advance_expired(today=None):
    """Daily job (and run when the dashboard or Board queue opens): holds that have ended are lifted, and cure periods that ended without cure move
    to the board queue. It does NOT hand held mail to the print service: only the daily job does that (send_due_mailings, its own step), never a page
    load, so a slow print service can never hold a page up or be called twice by two people opening the dashboard.
    Not one big transaction: each hold is lifted and each case moved on its own, and a case is moved only if it is STILL in its cure period under
    a row lock, so an extension granted a moment ago is never overwritten."""
    today = today or date.today()
    n = 0
    lift_expired_holds(today)
    for pk in list(Violation.objects.filter(status=S.NOTICE_SENT, cure_deadline__lt=today).values_list("pk", flat=True)):
        with transaction.atomic():
            v = Violation.objects.select_for_update().filter(pk=pk, status=S.NOTICE_SENT, cure_deadline__lt=today).first()
            if v is None:
                continue            # extended, closed or moved by someone else since the list was read: leave it alone
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


MAX_VOTES = 99          # a sanity limit on a typed vote count (a committee or Board is nowhere near this)


def _check_votes(votes_for, votes_against):
    """Vote counts are whole numbers of members, 0 or more. (A negative count used to slip through: 4 for and -1 against 'add up' to 3 votes and win.)"""
    if not all(isinstance(x, int) and 0 <= x <= MAX_VOTES for x in (votes_for, votes_against)):
        raise WorkflowError(f"Enter the votes as whole numbers of members, from 0 to {MAX_VOTES}.")


def _amount(value):
    """A fine typed by a person, as a Decimal: a plain number of dollars (cents allowed), never NaN or infinity, under $1,000,000. Empty means 0.
    Anything else is a plain message for the person, never a server error."""
    text = str(value if value is not None else "").strip()
    if not text:
        return Decimal(0)
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise WorkflowError("Enter the fine as a number of dollars, for example 100 or 100.50.") from None
    if not d.is_finite() or abs(d) >= 1_000_000 or d != d.quantize(Decimal("0.01")):
        raise WorkflowError("Enter the fine as a number of dollars, for example 100 or 100.50 (cents at most, under $1,000,000).")
    return d


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
                 suspension_days=None, by_majority=False):
    """Board decision at a meeting. 'proceed' issues the fine, which is only proposed until the Compliance Committee approves it at
    its hearing (FS 720.305(2)). It takes a majority of the Board: either confirm 'by_majority' (the Board approved this at its meeting)
    or give the votes. (Suspensions are still supported here but the app no longer offers them: the Association has no amenities.)
    A vote to go ahead moves the case on; a vote to dismiss it is a decision in the owner's favour, so not on your own household's case."""
    if decision == "dismiss":
        _not_your_own(v, user, "record a Board vote to dismiss it")
    _require(v, S.PENDING_BOARD)
    v.board_meeting_date = meeting_date
    if not settings.SUSPENSION_ENABLED:     # suspension is switched off: anything sent for it is ignored, never saved
        suspension, suspension_days = "", None
    if decision == "proceed":
        fine = Decimal(fine) if fine not in (None, "") else None
        suspension = (suspension or "").strip()
        if not fine and not suspension:
            raise WorkflowError("Enter the proposed fine amount" + (" and/or suspension" if settings.SUSPENSION_ENABLED else "") + " the Board voted on.")
        if not (by_majority or (votes_for is not None and votes_against is not None and votes_for > votes_against)):
            raise WorkflowError("A fine needs approval by a majority of the Board. Confirm that the Board approved it at its meeting "
                                "(or enter votes for and against, with more for than against).")
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
        log(v, f"Board voted {meeting_date:%b %d, %Y} ({f'{votes_for}-{votes_against}' if votes_for is not None else 'majority'}) to levy: {what}"
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


def nth_weekday(year, month, weekday, nth):
    """The date of the nth (1-4) or last (nth=-1) given weekday (Monday=0) of a month."""
    import calendar
    days = [d for d in calendar.Calendar().itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    return days[-1] if nth == -1 else days[nth - 1]


def series_dates(first_start, months):
    """The dates of a monthly series: the same weekday-of-the-month (3rd Monday...) and time as `first_start`, for `months` months.
    Returns (label, [aware datetimes])."""
    dt = timezone.localtime(first_start) if timezone.is_aware(first_start) else first_start
    nth = (dt.day - 1) // 7 + 1
    nth = -1 if nth == 5 else nth
    ords = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", -1: "last"}
    label = f"{ords[nth]} {dt:%A} of each month"
    out = []
    y, m = dt.year, dt.month
    for _ in range(months):
        day = nth_weekday(y, m, dt.weekday(), nth)
        naive = datetime.combine(day, dt.time())
        out.append(timezone.make_aware(naive) if timezone.is_naive(naive) else naive)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return label, out


def create_meeting_series(first_start, months, user=None, zoom_link="", location="", create_zoom=False, **kw):
    """Create a meeting for each month of a repeating series (each with its OWN Zoom meeting, so the hearing notice for a date carries
    that date's link). Stops at the first problem. Returns (meetings_created, error_message_or_empty)."""
    label, dates = series_dates(first_start, months)
    made = []
    for d in dates:
        try:
            made.append(create_meeting(d, zoom_link, location, user, create_zoom=create_zoom, series=label, **kw))
        except WorkflowError as e:
            return made, f"{timezone.localtime(d):%B} {timezone.localtime(d).day}: {e}"
    return made, ""


def create_meeting(starts_at, zoom_link="", location="", user=None, zoom_meeting_id="", zoom_passcode="", zoom_dial_in="", create_zoom=False, series=""):
    """Create a committee meeting. With create_zoom (and Zoom connected) the Zoom meeting is scheduled for you and its link, ID,
    passcode and dial-in numbers are filled in; they then appear on the hearing notices automatically."""
    from . import zoom
    if isinstance(starts_at, datetime) and timezone.is_naive(starts_at):
        starts_at = timezone.make_aware(starts_at)
    api_id = ""
    if create_zoom and not zoom_link:
        try:
            z = zoom.create_meeting(f"{settings.HOA_NAME} Compliance Committee hearings", starts_at)
        except zoom.ZoomError as e:
            if not location:
                raise WorkflowError(f"{e} Paste a Zoom link or add a location, then try again.")
            z = None
        if z:
            zoom_link, zoom_meeting_id, zoom_passcode, zoom_dial_in, api_id = z["link"], z["meeting_id"], z["passcode"], z["dial_in"], z["api_id"]
    if not (zoom_link or location):
        raise WorkflowError("Add a Zoom link and/or a location for the meeting.")
    m = CommitteeMeeting.objects.create(starts_at=starts_at, zoom_link=zoom_link, location=location, zoom_api_id=api_id, series=series,
                                        zoom_meeting_id=zoom_meeting_id, zoom_passcode=zoom_passcode, zoom_dial_in=zoom_dial_in)
    audit.record("meeting.created", _when(m), (f"series: {series}. " if series else "") + (f"Location: {location}. " if location else "") + ("Zoom link added." if zoom_link else ""), actor=user)
    return m


def _when(m):
    t = timezone.localtime(m.starts_at)
    return f"Meeting {t:%b} {t.day}, {t.year} {t:%I:%M %p}".replace(" 0", " ")


def _meeting_changeable(m):
    heard = [v for v in m.violations.all() if v.status != S.HEARING_SCHEDULED or v.committee_outcome]
    if heard or m.results_status != CommitteeMeeting.Results.OPEN:
        raise WorkflowError("The committee has already recorded decisions for this meeting, so it can't be moved or cancelled.")


def reschedule_meeting(m, new_start, user=None, notify=True):
    """Move a meeting. Its cases follow the new date, the Zoom meeting is moved too (same link), and, if cases are assigned and `notify`,
    each owner gets a fresh hearing notice (certified mail and email) with the new date. Returns a list of warnings.
    The meeting and every case on it are saved together (all or nothing) BEFORE anything is sent; a notice that then fails is reported as a
    warning and the others still go (it used to stop half way, leaving later cases on the old date)."""
    from . import zoom
    _meeting_changeable(m)
    if isinstance(new_start, datetime) and timezone.is_naive(new_start):
        new_start = timezone.make_aware(new_start)
    if new_start <= timezone.now():
        raise WorkflowError("Pick a date in the future.")
    cases = list(m.violations.select_related("owner", "vtype"))
    if cases and timezone.localtime(new_start).date() < date.today() + timedelta(days=settings.HEARING_NOTICE_DAYS):
        raise WorkflowError(f"Owners with cases on this meeting must get at least {settings.HEARING_NOTICE_DAYS} days' notice of the new date.")
    with transaction.atomic():
        _fresh(m, "starts_at")                   # the time it has NOW (a double click, or someone else, may have just moved it)
        if new_start == m.starts_at:
            raise WorkflowError("The meeting is already at that date and time, so nothing was changed and no new notices were sent.")
        old = timezone.localtime(m.starts_at)
        m.starts_at = new_start
        m.save()
        for v in cases:
            v.hearing_date = new_start
            v.save(update_fields=["hearing_date"])
            log(v, f"Hearing moved from {old:%b} {old.day} to {timezone.localtime(new_start):%b} {timezone.localtime(new_start).day}", user)
            cancel_queued_mailings(v, "the hearing was moved", kinds=(Notice.Kind.HEARING,))      # a hearing letter not yet handed over is out of date
        audit.record("meeting.rescheduled", _when(m), f"from {old:%b} {old.day}, {old.year} to {_when(m)[8:]}; {len(cases)} case(s); owners notified: {'yes' if notify and cases else 'no'}", actor=user)
    warnings = []
    try:
        zoom.update_meeting_time(m.zoom_api_id, new_start)
    except zoom.ZoomError as e:
        warnings.append(f"{e} Update the time in Zoom by hand.")
    if notify:
        for v in cases:
            try:
                send_notice(v, Notice.Kind.HEARING, user)
            except Exception as e:      # noqa: BLE001  (one owner's notice failing must not stop the others)
                log(v, f"WARNING: the new hearing notice could not be sent ({type(e).__name__}); send it again by hand.", user)
                warnings.append(f"The new hearing notice for case #{v.pk} ({v.owner.property_address}) could not be sent ({type(e).__name__}). Send it by hand.")
    if cases and not notify:
        warnings.append("The owners were not sent new hearing notices.")
    return warnings


@transaction.atomic
def cancel_meeting(m, user=None, notify=True):
    """Cancel a meeting that hasn't been heard: its cases go back to 'waiting to be scheduled', the owners are emailed, the Zoom meeting
    is deleted and the meeting is removed. Returns a list of warnings."""
    from . import zoom
    _meeting_changeable(m)
    when = timezone.localtime(m.starts_at)
    warnings = []
    for v in list(m.violations.select_related("owner", "vtype")):
        v.status, v.committee_meeting, v.hearing_date, v.hearing_location = S.HEARING_NEEDED, None, None, ""
        v.save()
        log(v, f"Hearing on {when:%b} {when.day} cancelled; waiting to be put on another meeting", user)
        close_unmailed_pieces(v, f"The hearing on {when:%b} {when.day} was cancelled.", user, kinds=(Notice.Kind.HEARING,))
        cancel_queued_mailings(v, f"the hearing on {when:%b} {when.day} was cancelled", kinds=(Notice.Kind.HEARING,))
        if notify:
            _email_owner(v, f"Hearing cancelled: {v.owner.property_address}",
                         f"The Compliance Committee hearing scheduled for {when:%A, %B} {when.day} at {when:%I:%M %p}".replace(" 0", " ") +
                         " has been cancelled. You will receive a new notice, by certified mail and email, when it is rescheduled. "
                         "You do not need to take any action for the cancelled date.")
    try:
        zoom.delete_meeting(m.zoom_api_id)
    except zoom.ZoomError as e:
        warnings.append(f"{e} Delete it in Zoom by hand.")
    audit.record("meeting.cancelled", _when(m), f"Cancelled; owners notified: {'yes' if notify else 'no'}", actor=user)
    m.delete()
    return warnings


@transaction.atomic
def assign_to_meeting(v, meeting, user=None, today=None):
    """Send a board-approved case to a Compliance Committee meeting: stage changes and the Notice of Hearing
    (with the Zoom link, proposed amount and the original notice) goes out by email and certified + regular mail."""
    _require(v, S.HEARING_NEEDED, S.HEARING_SCHEDULED)
    _fresh(v, "committee_meeting_id")            # the meeting it is on NOW: a double click (or another person) may have just put it there
    if v.status == S.HEARING_SCHEDULED and v.committee_meeting_id == meeting.pk:
        raise WorkflowError("This case is already on that meeting and its Notice of Hearing was already sent, so nothing was sent again. "
                            "(To move it to a different meeting, pick that one.)")
    today = today or date.today()
    min_day = today + timedelta(days=settings.HEARING_NOTICE_DAYS)
    if timezone.localtime(meeting.starts_at).date() < min_day:
        raise WorkflowError(f"Hearing must be at least {settings.HEARING_NOTICE_DAYS} days after notice "
                            f"(earliest {min_day:%b %d, %Y}).")
    v.committee_meeting, v.hearing_date = meeting, meeting.starts_at
    v.hearing_location = cut_to(meeting.place, Violation._meta.get_field("hearing_location").max_length)      # (a long Zoom link plus a long place can be longer than the column)
    v.status = S.HEARING_SCHEDULED
    v.save()
    log(v, f"Sent to Compliance Committee meeting {timezone.localtime(meeting.starts_at):%b %d, %Y %I:%M %p}", user)
    return send_notice(v, Notice.Kind.HEARING, user)


def assign_all_to_meeting(meeting, user=None, today=None, cases=None):
    """Batch: every board-approved case waiting for a hearing (or just the given `cases`) goes onto this meeting. Returns (done, skipped)."""
    done, skipped = [], []
    for v in (cases if cases is not None else Violation.objects.filter(status=S.HEARING_NEEDED)):
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
    _not_your_own(v, user, "record the committee's decision on it")
    _require(v, S.HEARING_SCHEDULED)
    if v.hearing_date and v.hearing_date > timezone.now():
        raise WorkflowError("The hearing date has not occurred yet.")
    _check_votes(votes_for, votes_against)
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


# ---- Compliance Committee meeting results: the committee works the list, the Board reviews and processes it ----
def conflicted_members(v):
    """Committee members for this case's meeting whose own login belongs to the violation's owner: it is their own (or their household's)
    violation, so they must abstain."""
    if v.committee_meeting_id is None:
        return []
    out = []
    for member in v.committee_meeting.roster():
        p = getattr(member.user, "profile", None) if member.user_id else None
        if (p is not None and p.owner_id == v.owner_id) or (member.owner_id and member.owner_id == v.owner_id) or lists_login_email(member.user, v.owner):
            out.append(member)
    return out


@transaction.atomic
def committee_record(v, outcome, user=None, amount=None, votes_for=None, votes_against=None, findings="", owner_present=False, abstain_ids=()):
    """The committee's decision on one case at its meeting (saved; the case does not move until the meeting's results are submitted).
    A case that is already resolved can be waived any time before the hearing; a fine needs the hearing to have happened and 3+ votes.
    All in one transaction with the case locked: the decision and the list of members who did not vote are saved together, and "Submit results" at the same moment
    waits for this to finish (or finds the case already submitted and refuses)."""
    _not_your_own(v, user, "record the committee's decision on it")
    _require(v, S.HEARING_SCHEDULED)
    m = v.committee_meeting
    if m is None or m.results_status != CommitteeMeeting.Results.OPEN:
        raise WorkflowError("This meeting's results are not open for changes.")
    if outcome not in dict(Violation.OUTCOMES):
        raise WorkflowError("Choose what the committee decided.")
    if not (findings or "").strip():
        raise WorkflowError("Add a short note or findings of fact for this case.")
    if outcome != "waive":
        if v.hearing_date and v.hearing_date > timezone.now():
            raise WorkflowError("The hearing has not happened yet. (A case that is already resolved can be waived before then.)")
        if votes_for is None or votes_against is None:
            raise WorkflowError("Enter the committee's votes: at least 3 members must vote.")
        _check_votes(votes_for, votes_against)
        if votes_for + votes_against < 3:
            raise WorkflowError("Enter the committee's votes: at least 3 members must vote.")
        if outcome in ("confirm", "amend") and votes_for <= votes_against:
            raise WorkflowError("A fine needs a majority of the committee in favor.")
        if outcome == "reject" and votes_against < votes_for:
            raise WorkflowError("Rejecting the fine means the committee did not vote in favor of it.")
    if outcome == "amend":
        amount = _amount(amount)
        if amount <= 0:
            raise WorkflowError("Enter the new fine amount.")
        if fine_problems(v, amount):
            raise WorkflowError(" ".join(fine_problems(v, amount)))
    v.committee_outcome = outcome
    v.committee_amended_fine = Decimal(amount) if outcome == "amend" else None
    if outcome == "waive":
        v.committee_votes_for = v.committee_votes_against = None
    else:
        v.committee_votes_for, v.committee_votes_against = votes_for, votes_against
    v.committee_findings = findings.strip()
    v.committee_owner_present = bool(owner_present)
    v.save()
    conflicts = {m.pk for m in conflicted_members(v)}
    roster = {m.pk: m for m in v.committee_meeting.roster()}
    v.abstentions.all().delete()
    chosen = {int(t) for t in (str(i).strip() for i in abstain_ids) if t.isascii() and t.isdigit() and len(t) <= 9 and int(t) in roster}      # (odd digits are simply ignored)
    for pk in conflicts | chosen:
        CaseAbstention.objects.create(violation=v, member=roster[pk], reason="Own violation" if pk in conflicts else "Abstained")
    log(v, f"Committee recorded: {dict(Violation.OUTCOMES)[outcome]}" + (f" (${v.committee_amended_fine})" if outcome == "amend" else ""), user)


@transaction.atomic
def submit_meeting_results(m, user=None):
    """The committee is done: every case needs a decision. Cases move to administrative review; nothing is imposed yet.
    The meeting and its cases are locked and re-read first: two people pressing Submit at once (or a decision saved at that moment) can
    neither submit twice nor lose a decision."""
    _fresh(m, "results_status")
    if m.results_status != CommitteeMeeting.Results.OPEN:
        raise WorkflowError("These results were already submitted.")
    cases = list(m.violations.select_for_update().filter(status=S.HEARING_SCHEDULED))
    if not cases:
        raise WorkflowError("There are no cases waiting for a decision at this meeting.")
    undecided = [v for v in cases if not v.committee_outcome]
    if undecided:
        raise WorkflowError(f"Record a decision for every case first ({len(undecided)} still open).")
    for v in cases:
        if v.committee_outcome in ("confirm", "amend"):
            v.fine_amount = v.committee_amended_fine if v.committee_outcome == "amend" else v.proposed_fine
            v.suspension, v.suspension_days = v.proposed_suspension, v.proposed_suspension_days
        else:
            v.fine_amount, v.suspension, v.suspension_days = None, "", None
        v.status = S.LEDGER_PENDING
        v.save()
        log(v, f"Committee results submitted for administrative review: {dict(Violation.OUTCOMES)[v.committee_outcome]}", user)
    m.results_status, m.submitted_at, m.submitted_by = CommitteeMeeting.Results.SUBMITTED, timezone.now(), user
    m.save()
    return cases


def board_review_item(v, user, action, final_outcome="", amount=None, note="", method="manual", reference=""):
    """Board / manager reviews one committee result. action: 'approve' (as recommended), 'modify' (lower the fine, or waive it instead)
    or 'return' (send it back to the committee). Approving a fine posts it to the ledger and sends the decision letter (Letter 3).
    On your own household's case only 'approve' of a FINE is open to you (it carries the committee's decision on to the next stage); lowering,
    waiving, sending back, or approving a waiver / rejection is for someone else."""
    _require(v, S.LEDGER_PENDING)
    if not (action == "approve" and v.committee_outcome in ("confirm", "amend")):         # a fine approved as decided is only moving the case on
        _not_your_own(v, user, "approve the committee's decision to waive or reject it" if action == "approve"
                      else "lower, waive or send back the committee's result")
    m = v.committee_meeting
    if m is None or m.results_status != CommitteeMeeting.Results.SUBMITTED or not v.committee_outcome:
        raise WorkflowError("This case is not waiting on a committee result.")
    committee = v.committee_outcome
    today = date.today()
    if action == "return":
        if not (note or "").strip():
            raise WorkflowError("Say what the committee should look at again.")
        v.status, v.committee_outcome, v.board_review_note = S.HEARING_SCHEDULED, "", note.strip()
        v.committee_findings = (v.committee_findings + f"\n[Returned by the Board: {note.strip()}]").strip()
        with transaction.atomic():          # the case goes back and the meeting is opened for the committee again, together or not at all
            _save_locked(v, S.LEDGER_PENDING)
            m.results_status, m.submitted_at, m.processed_at = CommitteeMeeting.Results.OPEN, None, None
            m.save()
            log(v, f"Board returned the committee result for another look: {note.strip()}", user)
        return None
    if action == "approve":
        final = committee
        fine = v.fine_amount
    elif action == "modify":
        if final_outcome == "waive":
            final, fine = "waive", None
        elif committee in ("confirm", "amend"):
            fine = _amount(amount)
            if fine <= 0:
                raise WorkflowError("Enter the new fine amount (or choose to waive it).")
            if v.proposed_fine and fine > v.proposed_fine:
                raise WorkflowError(f"The Board can reduce the fine but not raise it above ${v.proposed_fine}, the amount in the hearing notice.")
            if fine_problems(v, fine):
                raise WorkflowError(" ".join(fine_problems(v, fine)))
            final = "amend" if fine != v.proposed_fine else "confirm"
        else:
            raise WorkflowError("The Board can only lower a fine the committee confirmed, or waive it. To impose one, return it to the committee.")
    else:
        raise WorkflowError("Choose approve, modify or return.")
    v.board_outcome, v.board_final_fine, v.board_review_note = final, fine, (note or "").strip()
    v.board_reviewed_on, v.board_modified = today, (action == "modify")
    result, letter_problem = None, None
    if final in ("confirm", "amend"):
        v.fine_amount = fine
        _save_locked(v, S.LEDGER_PENDING)      # refused if another Board member already processed this case
        try:
            result = finalize_imposition(v, user, method, reference)
        except DecisionLetterFailed as e:      # the fine stands: finish the meeting's review below, then tell the person
            letter_problem = e
    elif final == "waive":
        v.status, v.fine_amount, v.suspension = S.WAIVED, None, ""
        _save_locked(v, S.LEDGER_PENDING)
        cancel_held_mailings(v, "waived by the Compliance Committee")
        _email_owner(v, f"Violation waived: {v.owner.property_address}",
                     f"The Compliance Committee reviewed the {v.vtype.name.lower()} violation at {v.owner.property_address} and waived it. "
                     "No fine" + (" or suspension" if settings.SUSPENSION_ENABLED else "") + " will be imposed and this matter is closed. "
                     "Thank you for your attention to it.")
        log(v, "Board processed: violation waived and closed" + (f" ({note.strip()})" if note and note.strip() else ""), user)
    else:   # the committee rejected the fine
        v.status, v.fine_amount, v.suspension = S.FINE_REJECTED, None, ""
        _save_locked(v, S.LEDGER_PENDING)
        log(v, "Board processed: committee rejected the fine; nothing imposed. Notice of committee decision sent.", user)
        try:
            result = send_notice(v, Notice.Kind.FINE, user)
        except Exception as e:      # noqa: BLE001
            try:
                _decision_letter_failed(v, user, e, "The committee's rejection was recorded and nothing is imposed.")
            except DecisionLetterFailed as problem:
                letter_problem = problem
    if not m.violations.filter(status=S.LEDGER_PENDING).exists():
        m.results_status, m.processed_at = CommitteeMeeting.Results.PROCESSED, timezone.now()
        m.save()
    if letter_problem is not None:
        raise letter_problem
    return result


def finalize_imposition(v, user=None, method="manual", reference="", today=None):
    """Board member / manager approves the Committee's decision: post the fine to the owner's ledger (Vantaca API, or record that
    it was posted by hand), then send the Notice of Imposition to the owner (and tenant). The fine is due FINE_PAYMENT_DAYS days
    after the letter date. The Committee's approval is what imposes the fine (no new Board vote); this step is the administrative
    review and posting.
    Posting is done once even if two people approve at the same moment: the case row is locked, the status is re-read, and the
    posting and the new status are saved together BEFORE any letter goes out. The second person is refused. (A fine already posted
    is also never posted twice on a retry.)"""
    today = today or date.today()
    with transaction.atomic():
        _require(v, S.LEDGER_PENDING)            # locks the row and re-reads the real status: a second approval stops here
        if v.committee_outcome in ("waive", "reject"):
            raise WorkflowError("This committee result is a waiver or rejection: process it from the committee results page.")
        v.refresh_from_db(fields=["ledger_posted_on", "ledger_reference"])      # the posting state as it is NOW, not as this page loaded it
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
            v.status = S.FINE_APPROVED         # saved in the same write as the posting
            v.fine_due_on = today + timedelta(days=settings.FINE_PAYMENT_DAYS + settings.FINE_MAIL_BUFFER_DAYS)
            v.suspension_effective_on = today if v.suspension else None
            v.save()
            log(v, f"Fine of ${v.fine_amount:,.2f} {how}" + (f" (ref {v.ledger_reference})" if v.ledger_reference else ""), user)
        else:
            v.status = S.FINE_APPROVED
            v.fine_due_on = today + timedelta(days=settings.FINE_PAYMENT_DAYS + settings.FINE_MAIL_BUFFER_DAYS) if v.fine_amount else None
            v.suspension_effective_on = today if v.suspension else None
            v.save()
        log(v, "Imposition approved" + (f"; fine due {v.fine_due_on:%b %d, %Y}" if v.fine_due_on else "") + ". Notice of committee decision (written findings) sent.", user)
    try:
        notice = send_notice(v, Notice.Kind.FINE, user)       # outside the transaction: a letter problem can never undo the posting
    except Exception as e:      # noqa: BLE001  (the fine IS imposed and posted: say so plainly and leave a warning, never a bare server error)
        _decision_letter_failed(v, user, e, "The fine was imposed and posted to the ledger.")
    _close_if_done(v, user)
    v.save()
    return notice


def _decision_letter_failed(v, user, error, what_stands):
    """Record, and raise, that the Notice of committee decision could not be created after the decision was saved."""
    logger.exception("the notice of committee decision for case %s could not be created", v.pk)
    log(v, f"WARNING: the Notice of committee decision could NOT be created or sent ({type(error).__name__}). {what_stands} The owner has NOT been sent this "
           "letter: it has to be sent by hand.", user)
    raise DecisionLetterFailed(f"{what_stands} But the Notice of committee decision could not be created or sent ({type(error).__name__}), so the owner has "
                               "NOT been sent it. This is recorded in the case history: the letter has to be sent by hand.") from None


# ---- a decision letter that never got made ----
DECISION_LETTER_STATUSES = (S.FINE_APPROVED, S.FINE_REJECTED)       # an imposed fine, or a fine the committee rejected: the owner is owed the Notice of committee decision


def _traced_decision_letters():
    """The Notice-of-decision rows that show the letter was emailed, is on its way by mail (queued, held, or a letter at the print service or listed for
    hand mailing, even a refused one: the red box deals with those) or at least that an email was tried (the Email log deals with that). An EMPTY row,
    which is what a failure half way through creating the letter leaves behind, shows nothing and does not count."""
    return Notice.objects.filter(kind=Notice.Kind.FINE).filter(
        Q(emailed_at__isnull=False) | Q(mailed_on__isnull=False) | Q(mail_queued_on__isnull=False) | Q(mail_hold_until__isnull=False)
        | ~Q(email_error="") | Q(pk__in=MailPiece.objects.values("notice_id")))


def decision_letter_missing(v):
    """True for an imposed or rejected case whose Notice of committee decision was never created or sent (see `DecisionLetterFailed`)."""
    return v.status in DECISION_LETTER_STATUSES and not _traced_decision_letters().filter(violation=v).exists()


def decision_letters_missing():
    """Every imposed or rejected case that has no decision letter on record, oldest first (the Board / manager home screen lists them)."""
    return (Violation.objects.filter(status__in=DECISION_LETTER_STATUSES).exclude(pk__in=_traced_decision_letters().values("violation_id"))
            .select_related("owner", "vtype").order_by("pk"))


def resend_decision_letter(v, user=None):
    """Create and send the Notice of committee decision for a case that has none (the first try failed and the history says so). It is a stage move,
    not a decision, so it is open to whoever may work the case, even on their own household's case. Safe to press twice: the case is locked and
    re-checked, and a case that already has its letter is refused with a plain message. If it fails again nothing half-made is kept (the same
    warning is written and `DecisionLetterFailed` raised)."""
    failure = notice = None
    with transaction.atomic():
        _require(v, *DECISION_LETTER_STATUSES)          # locks the case and re-reads its real status
        if not decision_letter_missing(v):
            raise WorkflowError("This case already has its Notice of committee decision on record (emailed, or on its way by mail), so nothing was sent "
                                "again. See 'Mailing log and print copies' on the case.")
        try:
            with transaction.atomic():
                notice = send_notice(v, Notice.Kind.FINE, user)
        except Exception as e:      # noqa: BLE001  (a letter problem is reported plainly, never as a bare server error)
            failure = e
        else:
            log(v, "Notice of committee decision sent again (the first one could not be created).", user)
    if failure is not None:
        _decision_letter_failed(v, user, failure, "The decision stands.")
    return notice


def owner_history(v, today=None, pool=None):
    """What else this owner/home has on record, for the staff view of a violation: how many times the same violation has come up,
    their other open violations, past ones, fines imposed, and whether this is a repeat within 3 months of a cure.
    `pool` ({owner id: that owner's cases, newest first, not drafts, each with its type}) lets a page that shows many cases (the Board queue)
    load them all at once instead of running its own queries for every case."""
    today = today or date.today()
    if pool is None:
        others = list(v.owner.violations.exclude(pk=v.pk).exclude(status=S.DRAFT).select_related("vtype").order_by("-created_at"))
    else:
        others = [o for o in pool.get(v.owner_id, ()) if o.pk != v.pk]
    open_states = set(Violation.OPEN) - {S.DRAFT}
    same_type = [o for o in others if o.vtype_id == v.vtype_id]
    same_section = [o for o in others if v.vtype.section and o.vtype.section == v.vtype.section and o.vtype.source == v.vtype.source
                    and o.vtype_id != v.vtype_id]
    open_others = [o for o in others if o.status in open_states]
    past = [o for o in others if o.status not in open_states]
    imposed = [o for o in others if o.fine_amount and o.status in (S.FINE_APPROVED, S.CLOSED, S.REFERRED_LEGAL)]
    this_issued = (next((n.created_at for n in v.notices.all() if n.kind == Notice.Kind.VIOLATION), None) or v.created_at).date()      # the first Notice of Violation
    repeat_of = next((o for o in same_type if o.cured_on and 0 <= (this_issued - o.cured_on).days <= 90), None)
    pit_prior = [o for o in others if o.non_curable]
    return {
        "pit_prior": pit_prior, "pit_count": len(pit_prior),
        "others": others, "same_type": same_type, "same_section": same_section, "open_others": open_others, "past": past,
        "total": len(others) + 1, "fines_total": sum(o.fine_amount for o in imposed),
        "last_12_months": sum(1 for o in others if o.created_at.date() >= today - timedelta(days=365)) + 1,
        "last_12_list": [o for o in others if o.created_at.date() >= today - timedelta(days=365)],
        "imposed": imposed,
        "occurrence": len(same_type) + 1,           # this is the 1st, 2nd, 3rd... time for this violation type
        "repeat_of": repeat_of,
    }
