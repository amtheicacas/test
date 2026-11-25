"""Open an encrypted database backup (for a restore or a restore rehearsal):

    python manage.py decrypt_backup hoa-2026-11-20-020000.dump.age backup-private-key.txt hoa.dump

Then: PostgreSQL dumps are restored with  pg_restore --no-owner --no-acl -d <new empty database> hoa.dump  (see BACKUP_AND_RECOVERY.md R2).
The standard `age` tool can open the same file:  age -d -i backup-private-key.txt hoa-....dump.age > hoa.dump"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from violations import dbbackup


class Command(BaseCommand):
    help = "Decrypt a database backup with the private key file."

    def add_arguments(self, p):
        p.add_argument("backup")
        p.add_argument("private_key_file")
        p.add_argument("output")

    def handle(self, *a, **o):
        out = Path(o["output"])
        if out.exists():
            raise CommandError(f"{out} already exists. Refusing to overwrite it.")
        try:
            data = dbbackup.decrypt(Path(o["backup"]).read_bytes(), Path(o["private_key_file"]).read_text(encoding="utf-8"))
        except dbbackup.BackupError as e:
            raise CommandError(str(e))
        except FileNotFoundError as e:
            raise CommandError(f"File not found: {e.filename}")
        except Exception as e:      # noqa: BLE001
            raise CommandError(f"Could not decrypt (wrong private key, or the file is damaged): {type(e).__name__}")
        out.write_bytes(data)
        self.stdout.write(f"Wrote {out} ({len(data):,} bytes).")
