from django.core.management.base import BaseCommand, CommandError

from violations.models import CommitteeMeeting, Violation
from violations.management import dev_guard


class Command(BaseCommand):
    help = ("DELETE every violation (with its letters, emails, messages, photos records and history) and every committee meeting, "
            "but keep owners, violation types, standard responses and logins. For clearing out test data before going live. Cannot be undone.")

    def add_arguments(self, p):
        p.add_argument("--yes", action="store_true", help="Required: confirms you really want to delete them.")

    def handle(self, *a, **o):
        dev_guard.test_site_only("clear_test_data")
        n_v, n_m = Violation.objects.count(), CommitteeMeeting.objects.count()
        if not o["yes"]:
            raise CommandError(f"This would delete {n_v} violation(s) and {n_m} committee meeting(s) permanently. "
                               "Run it again with --yes to confirm.")
        Violation.objects.all().delete()
        CommitteeMeeting.objects.all().delete()
        self.stdout.write(f"Deleted {n_v} violation(s) and {n_m} committee meeting(s). Owners, violation types and logins were kept. "
                          "Photo files remain in the media folder: delete that folder by hand if you want them gone.")
