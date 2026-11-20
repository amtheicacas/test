"""Database backup: dump the database, ENCRYPT it with a public key (age format), and store it in the backup bucket.

The server only ever holds the PUBLIC key, so it can write backups but cannot read them back; a stolen server key does not expose history.
The private key lives in two places chosen by the Board (see BACKUP_AND_RECOVERY.md). Nothing is ever overwritten or deleted here."""
import hashlib
import io
import os
import subprocess

from django.db import connection
from django.utils import timezone

PG_MAGIC, SQLITE_MAGIC = b"PGDMP", b"SQLite format 3\x00"


class BackupError(Exception):
    pass


def dump_database():
    """(bytes, kind): kind is 'postgres' (pg_dump custom format) or 'sqlite'. Refuses anything that does not look like a real dump."""
    s = connection.settings_dict
    engine = s["ENGINE"]
    if engine.endswith("postgresql"):
        cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-acl"]
        if s.get("HOST"):
            cmd += ["-h", str(s["HOST"])]
        if s.get("PORT"):
            cmd += ["-p", str(s["PORT"])]
        if s.get("USER"):
            cmd += ["-U", str(s["USER"])]
        cmd.append(str(s["NAME"]))
        env = dict(os.environ)
        if s.get("PASSWORD"):
            env["PGPASSWORD"] = str(s["PASSWORD"])           # in the environment, never on the command line
        if (s.get("OPTIONS") or {}).get("sslmode"):
            env["PGSSLMODE"] = s["OPTIONS"]["sslmode"]
        try:
            r = subprocess.run(cmd, capture_output=True, env=env, timeout=1800)       # noqa: S603
        except FileNotFoundError:
            raise BackupError("pg_dump is not installed on this machine (it must be the same version as the database server or newer).")
        if r.returncode != 0:
            raise BackupError("pg_dump failed: " + r.stderr.decode("utf-8", "replace").strip()[:500])
        data, kind, magic = r.stdout, "postgres", PG_MAGIC
    elif engine.endswith("sqlite3"):
        connection.ensure_connection()
        data = connection.connection.serialize()            # the whole database as one consistent block of bytes (Python 3.11+)
        kind, magic = "sqlite", SQLITE_MAGIC
    else:
        raise BackupError(f"Backups of {engine} are not supported.")
    if not data.startswith(magic) or len(data) < 1000:
        raise BackupError("The dump does not look like a real database dump, so it was not stored.")
    return data, kind


def encrypt(data, public_key):
    import pyrage
    try:
        recipient = pyrage.x25519.Recipient.from_str((public_key or "").strip())
    except Exception:       # noqa: BLE001
        raise BackupError("BACKUP_PUBLIC_KEY is not a valid age public key (it starts with 'age1').")
    return pyrage.encrypt(data, [recipient])


def decrypt(blob, identity_text):
    """For restore rehearsals: `identity_text` is the private key file's text (a line starting AGE-SECRET-KEY-)."""
    import pyrage
    line = next((x.strip() for x in identity_text.splitlines() if x.strip().startswith("AGE-SECRET-KEY-")), "")
    if not line:
        raise BackupError("No private key (a line starting AGE-SECRET-KEY-) was found in that file.")
    return pyrage.decrypt(blob, [pyrage.x25519.Identity.from_str(line)])


def object_name(kind, now=None):
    now = timezone.localtime(now or timezone.now())
    ext = "dump" if kind == "postgres" else "sqlite3"
    return f"database/hoa-{now:%Y-%m-%d-%H%M%S}.{ext}.age"


def store(target, blob, kind, now=None):
    """Write the encrypted backup, refusing to overwrite, and read it back to compare fingerprints. Returns (name, sha256)."""
    name = object_name(kind, now)
    if target.exists(name):
        raise BackupError(f"A backup named {name} already exists; nothing was written.")
    sha = hashlib.sha256(blob).hexdigest()
    target.put(name, blob, sha)
    if hashlib.sha256(target.get(name)).hexdigest() != sha:
        raise BackupError(f"{name} read back differently from what was sent: treat this backup as FAILED.")
    return name, sha
