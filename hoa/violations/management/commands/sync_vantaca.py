from django.core.management.base import BaseCommand

from violations import vantaca


class Command(BaseCommand):
    help = "Import/refresh homeowners from Vantaca (API by default, or --csv export). Safe to run daily."

    def add_arguments(self, p):
        p.add_argument("--csv", help="Path to a Vantaca owner export instead of calling the API.")

    def handle(self, *a, csv=None, **kw):
        if csv:
            with open(csv, "rb") as f:
                r = vantaca.sync_csv(f)
        else:
            r = vantaca.sync_api()
        self.stdout.write(f"created {r['created']}, updated {r['updated']}, skipped {r['skipped']}")
