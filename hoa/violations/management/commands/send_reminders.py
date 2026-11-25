from django.core.management.base import BaseCommand

from violations import services


class Command(BaseCommand):
    help = "Email owners as their cure deadline approaches (REMINDER_DAYS). Safe to run daily or more often: never double-sends."

    def handle(self, *a, **kw):
        self.stdout.write(f"{services.send_cure_reminders()} reminder(s) sent.")
