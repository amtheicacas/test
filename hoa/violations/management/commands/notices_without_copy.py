from django.core.management.base import BaseCommand

from violations import letters


class Command(BaseCommand):
    help = ("List the letters (notices) that have no saved exact copy: everything sent before copies were kept. Read-only; changes nothing. "
            "Use it to decide how to handle them (upload the original PDF by hand, or a rebuilt copy clearly marked as rebuilt).")

    def handle(self, *a, **kw):
        qs = letters.without_saved_copy()
        total = qs.count()
        self.stdout.write(f"{total} notice(s) have no saved exact copy.")
        by_kind = {}
        for n in qs:
            by_kind[n.get_kind_display()] = by_kind.get(n.get_kind_display(), 0) + 1
        for kind, n in sorted(by_kind.items()):
            self.stdout.write(f"  {kind}: {n}")
        for n in qs[:50]:
            self.stdout.write(f"  case #{n.violation_id} {n.violation.owner.property_address}: {n.get_kind_display()} created {n.created_at:%Y-%m-%d}"
                              + (f", emailed {n.emailed_at:%Y-%m-%d}" if n.emailed_at else "") + (f", mailed {n.mailed_on}" if n.mailed_on else ""))
        if total > 50:
            self.stdout.write(f"  ... and {total - 50} more")
