from django.core.management.base import BaseCommand

from violations import services


class Command(BaseCommand):
    help = "Run once a day: refer expired cure periods to the board and send deadline reminders."

    def handle(self, *a, **kw):
        self.stdout.write(f"{services.send_cure_reminders()} reminder(s) sent.")
        self.stdout.write(f"{services.advance_expired()} violation(s) referred to the board.")
