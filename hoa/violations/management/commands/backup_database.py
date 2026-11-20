"""Back up the database: dump, encrypt with the PUBLIC key, store in the backup bucket under database/.

    python manage.py backup_database             do it
    python manage.py backup_database --dry-run   dump and encrypt, report sizes, store nothing

Never overwrites or deletes. Exits with an error (so a scheduler can alert) if anything goes wrong."""
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from violations import backups, dbbackup


class Command(BaseCommand):
    help = "Encrypted database backup to the backup bucket (copy only; never overwrites or deletes)."

    def add_arguments(self, p):
        p.add_argument("--dry-run", action="store_true", help="Dump and encrypt, but store nothing.")

    def handle(self, *a, **o):
        s = settings
        if not s.BACKUP_PUBLIC_KEY:
            raise CommandError("BACKUP_PUBLIC_KEY is not set. Create the key pair first with:  python manage.py make_backup_key")
        if not o["dry_run"] and not (s.BACKUP_BUCKET and s.BACKUP_ACCESS_KEY_ID and s.BACKUP_SECRET_ACCESS_KEY):
            raise CommandError("The backup bucket is not set up: BACKUP_BUCKET, BACKUP_ENDPOINT_URL, BACKUP_ACCESS_KEY_ID and BACKUP_SECRET_ACCESS_KEY must be set.")
        w = self.stdout.write
        try:
            data, kind = dbbackup.dump_database()
            blob = dbbackup.encrypt(data, s.BACKUP_PUBLIC_KEY)
            w(f"Dumped the database ({kind}, {len(data):,} bytes) and encrypted it ({len(blob):,} bytes).")
            if o["dry_run"]:
                w("Dry run finished (nothing was stored).")
                return
            target = backups.S3Target(s.BACKUP_BUCKET, s.BACKUP_ENDPOINT_URL, s.BACKUP_REGION, s.BACKUP_ACCESS_KEY_ID, s.BACKUP_SECRET_ACCESS_KEY)
            name, sha = dbbackup.store(target, blob, kind)
        except dbbackup.BackupError as e:
            raise CommandError(str(e))
        except Exception as e:      # noqa: BLE001
            raise CommandError(f"The database backup could not run: {type(e).__name__}: {e}")
        w(f"Stored {s.BACKUP_BUCKET}/{name} (fingerprint {sha[:16]}). Backup complete.")
        if s.BACKUP_DB_HEARTBEAT_URL:
            try:
                urllib.request.urlopen(s.BACKUP_DB_HEARTBEAT_URL, timeout=15).read()      # noqa: S310  (the monitor address the Board chose)
            except Exception as e:      # noqa: BLE001
                w(f"  (the heartbeat monitor could not be reached: {type(e).__name__}; the backup itself is fine)")
