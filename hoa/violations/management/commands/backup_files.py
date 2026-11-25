"""Copy every uploaded file (letters, photos, minutes, site-visit photos) from the live storage to the backup bucket.

    python manage.py backup_files               copy what is missing, then check the database's files are all in the backup
    python manage.py backup_files --dry-run     only say what would be copied
    python manage.py backup_files --verify      also re-read files already in the backup and compare fingerprints

COPY ONLY: nothing is ever overwritten or deleted in the backup. Exits with an error (so a scheduler can alert) if anything went wrong."""
import urllib.request

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from violations import backups


class Command(BaseCommand):
    help = "Copy the uploaded files to the backup bucket (copy only; never overwrites or deletes)."

    def add_arguments(self, p):
        p.add_argument("--dry-run", action="store_true", help="Only report what would be copied.")
        p.add_argument("--verify", action="store_true", help="Also re-read files already in the backup and compare fingerprints (slower).")
        p.add_argument("--skip-database-check", action="store_true", help="Do not check that every file the database refers to is in the backup.")

    def handle(self, *a, **o):
        s = settings
        if not (s.BACKUP_BUCKET and s.BACKUP_ACCESS_KEY_ID and s.BACKUP_SECRET_ACCESS_KEY):
            raise CommandError("The backup bucket is not set up: BACKUP_BUCKET, BACKUP_ENDPOINT_URL, BACKUP_ACCESS_KEY_ID and "
                               "BACKUP_SECRET_ACCESS_KEY must be set (in hoa/local.live.env on the PC, or the host's environment).")
        src = s.STORAGES["default"]["OPTIONS"] if s.STORAGES["default"]["BACKEND"].endswith("S3Storage") else {}
        if src and src.get("bucket_name") == s.BACKUP_BUCKET and (src.get("endpoint_url") or "") == s.BACKUP_ENDPOINT_URL:
            raise CommandError("The backup bucket is the same as the live bucket. A backup must be somewhere else.")
        target = backups.S3Target(s.BACKUP_BUCKET, s.BACKUP_ENDPOINT_URL, s.BACKUP_REGION, s.BACKUP_ACCESS_KEY_ID, s.BACKUP_SECRET_ACCESS_KEY)
        self.stdout.write(f"Copying from {src.get('bucket_name') or 'the local media folder'} to {s.BACKUP_BUCKET} ({'dry run' if o['dry_run'] else 'copy only'})...")
        try:
            rep = backups.copy_files(default_storage, target, dry_run=o["dry_run"], verify=o["verify"])
            if not o["dry_run"] and not o["skip_database_check"]:
                backups.check_database(rep, target)
        except Exception as e:      # noqa: BLE001
            raise CommandError(f"The backup could not run: {type(e).__name__}: {e}")
        w = self.stdout.write
        w(f"  copied now:            {len(rep.copied)} ({rep.bytes_copied:,} bytes)")
        w(f"  already in the backup: {len(rep.present)}")
        if o["dry_run"]:
            w(f"  would copy:            {len(rep.would_copy)}")
            for n in rep.would_copy[:20]:
                w(f"      {n}")
        w(f"  skipped (test files):  {len(rep.skipped)}")
        for n in rep.conflicts:
            w(f"  CONFLICT (backup has a different file under this name, left alone): {n}")
        for n, why in rep.failures:
            w(f"  FAILED {n}: {why}")
        for n in rep.missing_from_backup[:50]:
            w(f"  MISSING FROM BACKUP (the database refers to it): {n}")
        if len(rep.missing_from_backup) > 50:
            w(f"  ... and {len(rep.missing_from_backup) - 50} more")
        if not rep.ok:
            raise CommandError("The backup is NOT complete: see the lines above.")
        w("Backup complete." if not o["dry_run"] else "Dry run finished (nothing was written).")
        if s.BACKUP_HEARTBEAT_URL and not o["dry_run"]:
            try:
                urllib.request.urlopen(s.BACKUP_HEARTBEAT_URL, timeout=15).read()      # noqa: S310  (the monitor address the Board chose)
            except Exception as e:      # noqa: BLE001
                w(f"  (the heartbeat monitor could not be reached: {type(e).__name__}; the backup itself is fine)")
