from django.core.management import call_command
from django.core.management.base import BaseCommand

from violations import accounts, services


class Command(BaseCommand):
    help = "Run once a day: refer expired cure periods to the board and send deadline reminders."

    def handle(self, *a, **kw):
        on, off = accounts.sync_committee_logins()
        self.stdout.write(f"Committee logins: {on} switched on, {off} switched off (they follow the yearly roster).")
        self.stdout.write(f"{services.refresh_mail_tracking()} certified letter(s) had tracking updates.")
        self.stdout.write(f"{services.send_due_mailings()} held mailing(s) sent to the mail provider.")
        self.stdout.write(f"{services.send_hold_reminders()} hold-ending reminder(s) sent.")
        self.stdout.write(f"{services.send_cure_reminders()} reminder(s) sent.")
        self.stdout.write(f"{services.advance_expired()} violation(s) referred to the board.")
        self.stdout.write(f"{accounts.purge_old_links()} old sign-in link(s) cleared.")
        call_command("clearsessions")        # signed-out and expired sessions
