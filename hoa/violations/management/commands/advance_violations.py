from django.core.management.base import BaseCommand

from violations import services


class Command(BaseCommand):
    help = "Move violations whose cure period expired into the board queue. Run daily from cron."

    def handle(self, *a, **kw):
        self.stdout.write(f"{services.advance_expired()} violation(s) referred to the board.")
