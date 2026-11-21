from django.core.management.base import BaseCommand, CommandError

from violations import vantaca


class Command(BaseCommand):
    help = "Import/refresh homeowners from Vantaca (API by default, or --csv export). Safe to run daily."

    def add_arguments(self, p):
        p.add_argument("--csv", help="Path to a Vantaca owner export instead of calling the API.")
        p.add_argument("--invite", action="store_true", help="Email new owners a link to create their login.")
        p.add_argument("--deactivate-missing", action="store_true",
                       help="Owners absent from this FULL feed are marked inactive and lose access.")
        p.add_argument("--force", action="store_true", help="Allow deactivating more than 20%% of owners.")

    def handle(self, *a, csv=None, invite=False, deactivate_missing=False, force=False, **kw):
        opts = dict(invite=invite, deactivate_missing=deactivate_missing, force=force)
        try:
            if csv:
                with open(csv, "rb") as f:
                    r = vantaca.sync_csv(f, **opts)
            else:
                r = vantaca.sync_api(**opts)
        except vantaca.SafetyStop as e:
            raise CommandError(str(e))
        self.stdout.write(", ".join(f"{k} {v}" for k, v in r.items()))
        if r.get("failed"):
            raise CommandError(f"{r['failed']} row(s) could not be imported (the others were); see the Audit log for which. The scheduler should flag this run.")
