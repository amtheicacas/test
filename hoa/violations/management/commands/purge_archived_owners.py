from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from violations.models import Owner, Profile, Violation


class Command(BaseCommand):
    help = ("List (and with --yes DELETE) former owners who have been inactive longer than ARCHIVE_RETENTION_YEARS, together with their "
            "violations, letters and logins. Nothing is deleted without --yes. Run it by hand when you decide to clear old records.")

    def add_arguments(self, p):
        p.add_argument("--yes", action="store_true", help="Actually delete. Without it this only lists what would go.")

    def handle(self, *a, **o):
        due = [x for x in Owner.objects.filter(active=False, inactive_since__isnull=False) if x.keep_until and x.keep_until <= date.today()]
        if not due:
            self.stdout.write(f"No former owner has been inactive for more than {settings.ARCHIVE_RETENTION_YEARS} years.")
            return
        for x in due:
            n = x.violations.count()
            self.stdout.write(f"{x.name}, {x.property_address}: inactive since {x.inactive_since}, {n} violation(s)")
        if not o["yes"]:
            self.stdout.write(f"\n{len(due)} record(s) would be deleted. Run again with --yes to delete them (this cannot be undone).")
            return
        User = get_user_model()
        for x in due:
            users = list(User.objects.filter(profile__owner=x, profile__role="homeowner"))
            Violation.objects.filter(owner=x).delete()
            x.delete()
            for u in users:
                u.delete()
        self.stdout.write(f"Deleted {len(due)} former owner record(s) and their violations.")
