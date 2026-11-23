from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from violations import accounts, services


class Command(BaseCommand):
    help = ("Run once a day: refer expired cure periods to the board and send deadline reminders. Every step runs even if an earlier one "
            "fails; if any step failed the job ends with an error (so a scheduler alerts) after the others have been done.")

    def handle(self, *a, **kw):
        failed, tracking_errors = [], []

        def step(name, fn, line):
            try:
                self.stdout.write(line(fn()))
            except Exception as e:      # noqa: BLE001  (one bad record must not stop the other daily jobs)
                failed.append(name)
                self.stdout.write(self.style.ERROR(f"FAILED {name}: {type(e).__name__}: {e}"))

        step("sync_committee_logins", accounts.sync_committee_logins,
             lambda r: f"Committee logins: {r[0]} switched on, {r[1]} switched off (they follow the yearly roster).")
        step("refresh_mail_tracking", lambda: services.refresh_mail_tracking(errors=tracking_errors),
             lambda n: f"{n} certified letter(s) had tracking updates."
             + (f" {len(tracking_errors)} tracking check(s) could NOT be completed (first: {tracking_errors[0]}); see the server log." if tracking_errors else ""))
        step("send_due_mailings", lambda: services.send_due_mailings(), lambda n: f"{n} mailing(s) handed to the print service (held copies whose wait ended, and any letters still queued).")
        step("send_hold_reminders", lambda: services.send_hold_reminders(), lambda n: f"{n} hold-ending reminder(s) sent.")
        step("send_cure_reminders", lambda: services.send_cure_reminders(), lambda n: f"{n} reminder(s) sent.")
        step("advance_expired", lambda: services.advance_expired(), lambda n: f"{n} violation(s) referred to the board.")
        step("purge_old_links", accounts.purge_old_links, lambda n: f"{n} old sign-in link(s) cleared.")
        step("clearsessions", lambda: call_command("clearsessions"), lambda _: "Signed-out and expired sessions cleared.")   # signed-out and expired sessions
        if failed:
            raise CommandError("These daily steps failed: " + ", ".join(failed) + ". The others were done.")
