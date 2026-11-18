from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from violations.models import CommitteeMeeting


class Command(BaseCommand):
    help = ("TESTING ONLY: pretend a committee meeting already happened (moves it, and the hearing date of its cases, to one hour ago) so the "
            "committee can record decisions today instead of waiting the 14+ days a real hearing needs.")

    def add_arguments(self, p):
        p.add_argument("meeting_id", type=int, nargs="?", help="Meeting id (default: the newest meeting).")

    def handle(self, *a, **o):
        m = CommitteeMeeting.objects.filter(pk=o["meeting_id"]).first() if o["meeting_id"] else CommitteeMeeting.objects.order_by("-created_at").first()
        if not m:
            raise CommandError("No committee meeting found. Create one under Committee meetings first.")
        when = timezone.now() - timedelta(hours=1)
        m.starts_at = when
        m.save()
        n = m.violations.update(hearing_date=when)
        self.stdout.write(f"Meeting {m.pk} now shows as held an hour ago; {n} case(s) updated. Log in as the committee member and open the meeting.")
