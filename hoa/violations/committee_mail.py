"""Emails to the Compliance Committee members (built 2026-11-25 after the owner's "go").

Who: the members on the roster for the meeting's year who are still active and have an email address (their login's address, else the owner record's).
Each person gets their OWN email, addressed by name. When: a meeting is set (ONE email for a whole repeating series), moved or cancelled, and once more
`COMMITTEE_REMINDER_DAYS` (default 7) days before a meeting that has cases on its agenda (`send_reminders`, run by the daily job; sent once per meeting).
What: dates, places, links and COUNTS only. An email never names an owner or an address and never describes a case: the cases are read on the meeting
page, behind the committee member's own login and two-step code. `EMAIL_TO_COMMITTEE=0` switches all of it off; `COMMITTEE_REMINDER_DAYS=0` only the reminder.
A change to a meeting is announced only once the change is saved for good (`transaction.on_commit`: an undone change sends nothing), and an email that
cannot be sent is logged and never stops the meeting from being saved. Never run against a real mail server from here: tests use Django's outbox."""
import logging
from datetime import date, timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import audit, mailer
from .models import CommitteeMeeting, Violation

logger = logging.getLogger(__name__)


def when(t):
    """'Monday, December 15 at 6:00 PM' (local time; no platform-specific date flags: this runs on Windows too)."""
    t = timezone.localtime(t)
    return f"{t:%A, %B} {t.day} at {t:%I:%M %p}".replace(" 0", " ")


def recipients(meeting):
    """[(name, email)] for the meeting's roster: active members that have an address, each address once (any capitals)."""
    out, seen = [], set()
    for member in meeting.roster():
        address = ""
        if member.user_id and member.user.email:
            address = member.user.email
        elif member.owner_id and member.owner.email:
            address = member.owner.email
        else:
            profile = getattr(member.user, "profile", None) if member.user_id else None
            if profile is not None and profile.owner_id and profile.owner.email:
                address = profile.owner.email
        address = address.strip()
        if address and address.lower() not in seen:
            seen.add(address.lower())
            out.append((member.name, address))
    return out


def _page(m):
    return f"{settings.SITE_URL}{reverse('committee_meeting', args=[m.pk])}"


def _where(m):
    lines = []
    if m.zoom_link:
        lines.append(f"Zoom: {m.zoom_link}")
        if m.zoom_meeting_id:
            lines.append(f"Meeting ID: {m.zoom_meeting_id}" + (f"   Passcode: {m.zoom_passcode}" if m.zoom_passcode else ""))
    if m.location:
        lines.append(f"Place: {m.location}")
    return "\n".join(lines) or "Place: to be announced"


def _send(label, kind, subject, make_body, people, user=None):
    """One email per person; returns how many were handed to the mail system. A failure is logged, never raised."""
    from .services import _signature
    sent = 0
    for name, address in people:
        try:
            mailer.send(EmailMessage(subject, f"Dear {name},\n\n{make_body()}{_signature()}", settings.DEFAULT_FROM_EMAIL, [address]))
            sent += 1
        except Exception:      # noqa: BLE001  (a mail problem must never stop a meeting from being saved)
            logger.exception("a committee email could not be sent")
    if people:
        audit.record("meeting.committee_emailed", label, f"{kind}: {sent} of {len(people)} committee member(s) emailed", actor=user)
    return sent


def meeting_set(meetings, user=None):
    """A meeting, or a whole series of them, was created. Asked for inside the request; sent once the work is saved."""
    if not settings.EMAIL_TO_COMMITTEE or not meetings:
        return
    meetings = list(meetings)
    people = recipients(meetings[0])
    if not people:
        return
    first = meetings[0]

    def body():
        if len(meetings) == 1:
            return (f"A Compliance Committee meeting has been scheduled for {when(first.starts_at)}.\n\n{_where(first)}\n\n"
                    f"Cases are added to the agenda by the Board. You will get a reminder {settings.COMMITTEE_REMINDER_DAYS} days before, saying how many "
                    f"cases are on it.\nMeeting page (sign in as usual): {_page(first)}")
        lines = "\n".join(f"  - {when(x.starts_at)}: {_page(x)}" for x in meetings)
        return (f"{len(meetings)} Compliance Committee meetings have been scheduled:\n\n{lines}\n\n{_where(first)}\n\n"
                "Cases are added to the agenda by the Board. You will get a reminder before each meeting that has cases on it.")
    subject = (f"Compliance Committee meeting scheduled: {when(first.starts_at)}" if len(meetings) == 1
               else f"{len(meetings)} Compliance Committee meetings scheduled, starting {when(first.starts_at)}")
    transaction.on_commit(lambda: _send(f"Meeting {when(first.starts_at)}", "meeting set", subject, body, people, user))


def meeting_moved(m, old_start, user=None):
    if not settings.EMAIL_TO_COMMITTEE:
        return
    people = recipients(m)
    if not people:
        return
    old = when(old_start)
    transaction.on_commit(lambda: _send(
        f"Meeting {when(m.starts_at)}", "meeting moved", f"Compliance Committee meeting moved to {when(m.starts_at)}",
        lambda: f"The Compliance Committee meeting that was set for {old} has been MOVED to {when(m.starts_at)}.\n\n{_where(m)}\n\nMeeting page: {_page(m)}",
        people, user))


def meeting_cancelled(m, user=None):
    """Call BEFORE the meeting is deleted (the roster comes from its date); the email is sent once the cancellation is saved."""
    if not settings.EMAIL_TO_COMMITTEE:
        return
    people, old = recipients(m), when(m.starts_at)
    if not people:
        return
    transaction.on_commit(lambda: _send(
        f"Meeting {old}", "meeting cancelled", f"Compliance Committee meeting cancelled: {old}",
        lambda: f"The Compliance Committee meeting that was set for {old} has been CANCELLED. You do not need to do anything for that date.",
        people, user))


def send_reminders(today=None):
    """Daily job: email the committee once about each meeting that is within COMMITTEE_REMINDER_DAYS days, has at least one case still waiting for the
    committee's decision, and has not been reminded. If the job missed days, the reminder still goes (once). Returns how many meetings were reminded."""
    days = settings.COMMITTEE_REMINDER_DAYS
    if not settings.EMAIL_TO_COMMITTEE or days <= 0:
        return 0
    today, now, done = today or date.today(), timezone.now(), 0
    for m in CommitteeMeeting.objects.filter(starts_at__gt=now, starts_at__lt=now + timedelta(days=days + 1), committee_reminded_on__isnull=True):
        if (timezone.localtime(m.starts_at).date() - today).days > days:
            continue
        waiting = m.violations.filter(status=Violation.Status.HEARING_SCHEDULED).count()
        people = recipients(m) if waiting else []
        if not people:
            continue         # nothing to review, or nobody to tell yet: look again tomorrow
        if not CommitteeMeeting.objects.filter(pk=m.pk, committee_reminded_on__isnull=True).update(committee_reminded_on=today):
            continue         # another run just did it
        sent = _send(f"Meeting {when(m.starts_at)}", "reminder", f"Reminder: Compliance Committee meeting, {when(m.starts_at)}",
                     lambda: (f"Your Compliance Committee meeting is on {when(m.starts_at)}. "
                              f"{waiting} case{' is' if waiting == 1 else 's are'} on the agenda.\n\n{_where(m)}\n\n"
                              f"Please open the meeting page and read the case{'' if waiting == 1 else 's'} before the meeting: {_page(m)}\n"
                              "(Sign in with your usual login and two-step code. The cases are not described in this email on purpose.)"), people)
        if sent:
            done += 1
        else:
            CommitteeMeeting.objects.filter(pk=m.pk).update(committee_reminded_on=None)       # nothing could be sent: try again on the next run
    return done
