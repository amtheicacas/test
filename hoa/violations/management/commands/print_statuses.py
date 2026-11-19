from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from violations import progress
from violations.models import Owner, Violation, ViolationType


class Command(BaseCommand):
    help = "Print the statuses, what owners see and what the Board / Manager see, as a table (for review). Changes nothing."

    def handle(self, *a, **o):
        S = Violation.Status
        out = []
        with transaction.atomic():
            owner = Owner.objects.create(name="Sample Owner", property_address="1 Sample Way")
            vt = ViolationType.objects.create(name="Sample", default_fine=100)
            now = timezone.now()
            base = dict(owner=owner, vtype=vt, description="d", cure_required="c", observed_on=date.today())
            ex = {
                S.DRAFT: {},
                S.NOTICE_SENT: dict(cure_deadline=date.today() + timedelta(days=10)),
                S.PENDING_BOARD: dict(cure_deadline=date.today() - timedelta(days=3)),
                S.HEARING_NEEDED: dict(board_meeting_date=date.today(), proposed_fine=100),
                S.HEARING_SCHEDULED: dict(board_meeting_date=date.today(), proposed_fine=100, hearing_date=now + timedelta(days=20)),
                S.LEDGER_PENDING: dict(board_meeting_date=date.today(), proposed_fine=100, fine_amount=100, hearing_date=now - timedelta(days=1), committee_outcome="confirm"),
                S.FINE_APPROVED: dict(board_meeting_date=date.today(), proposed_fine=100, fine_amount=100, fine_due_on=date.today() + timedelta(days=35),
                                      hearing_date=now - timedelta(days=5), committee_outcome="confirm", ledger_posted_on=date.today()),
                S.FINE_REJECTED: dict(board_meeting_date=date.today(), proposed_fine=100, hearing_date=now - timedelta(days=5), committee_outcome="reject"),
                S.WAIVED: dict(board_meeting_date=date.today(), proposed_fine=100, hearing_date=now - timedelta(days=5), committee_outcome="waive"),
                S.CURED: dict(cured_on=date.today()),
                S.CLOSED: dict(board_meeting_date=date.today(), fine_amount=100, fine_paid_on=date.today(), cured_on=date.today(), ledger_posted_on=date.today()),
                S.DISMISSED: {},
                S.ON_HOLD: dict(held_from=S.NOTICE_SENT, hold_until=date.today() + timedelta(days=14), cure_deadline=date.today() + timedelta(days=10)),
                S.REFERRED_LEGAL: dict(legal_path="direct", legal_referred_on=date.today()),
            }
            out.append("| Internal status | Staff label | Owner pill | Current step (owner) | What the owner reads | Staff: current step and its sub-steps |")
            out.append("|---|---|---|---|---|---|")
            for key, label in S.choices:
                v = Violation.objects.create(status=key, **base, **ex[key])
                if key == S.DRAFT:
                    owner_view = "(not visible)"
                    cur = "-"
                    text = ""
                    staff = "-"
                else:
                    steps = v.progress
                    cur_step = next((s for s in steps if s["state"] == "current"), steps[-1])
                    cur, text = cur_step["title"], cur_step["text"]
                    owner_view = v.portal_status[0]
                    staff_steps = v.staff_progress
                    sp = next((s for s in staff_steps if s["state"] == "current"), None) or next(s for s in staff_steps if s["key"] == cur_step["key"])
                    staff = "**" + sp["title"] + "**: " + "; ".join(f"{u['label']} ({u['state']})" for u in sp["subs"])
                out.append(f"| {key} | {label} | {owner_view} | {cur} | {text} | {staff} |")
            out.append("")
            out.append("**The six steps and their sub-steps (Board / Manager view)**")
            out.append("")
            v = Violation.objects.create(status=S.FINE_APPROVED, **base, **ex[S.FINE_APPROVED])
            for s in v.staff_progress:
                out.append(f"- **{s['title']}**: " + "; ".join(u["label"] for u in s["subs"]))
            transaction.set_rollback(True)
        self.stdout.write("\n".join(out))
