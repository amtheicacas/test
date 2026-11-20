"""Create the key pair for database backups, on THIS computer.

    python manage.py make_backup_key

The PUBLIC key is printed (it is not secret: it goes into BACKUP_PUBLIC_KEY). The PRIVATE key is written to a file and never shown on screen:
move that file into the password manager (and give the sealed copy to the second Board officer), then delete it from this computer.
Without the private key no backup can ever be read, so do this carefully."""
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Create the backup key pair on this computer (the private key goes to a file, never to the screen)."

    def add_arguments(self, p):
        p.add_argument("--file", default="backup-private-key.txt", help="Where to write the private key file.")

    def handle(self, *a, **o):
        import pyrage
        path = Path(o["file"])
        if path.exists():
            raise CommandError(f"{path} already exists. Refusing to overwrite a key file: move or rename it first.")
        ident = pyrage.x25519.Identity.generate()
        public = str(ident.to_public())
        text = f"# created: {timezone.now():%Y-%m-%d}\n# public key: {public}\n{ident}\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.stdout.write(f"Public key (not secret): {public}\n")
        self.stdout.write(f"Add this line to local.live.env (and to the host's settings):  BACKUP_PUBLIC_KEY={public}\n")
        self.stdout.write(f"The PRIVATE key was written to {path.resolve()}. It is not shown here.\n"
                          "Next: open that file in Notepad, copy its whole contents into the password manager, give the sealed copy to the second "
                          "Board officer, then delete the file from this computer. If it is lost, no backup can be read.\n")
