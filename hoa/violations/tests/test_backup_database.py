"""Encrypted database backup: dump -> encrypt with the PUBLIC key -> store; never overwrites; the standard `age` tool can open it."""
import shutil
import sqlite3
import subprocess
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from violations import backups, dbbackup
from violations.models import Owner

pytestmark = pytest.mark.django_db
pyrage = pytest.importorskip("pyrage")


@pytest.fixture
def keys():
    ident = pyrage.x25519.Identity.generate()
    return str(ident.to_public()), f"# public key\n{ident}\n"


class Mem:
    def __init__(self, files=None, corrupt=False):
        self.files, self.corrupt = dict(files or {}), corrupt

    def exists(self, k): return k in self.files
    def put(self, k, data, sha): self.files[k] = data + (b"x" if self.corrupt else b"")
    def get(self, k): return self.files[k]


@pytest.fixture
def conf(settings, keys):
    settings.BACKUP_BUCKET, settings.BACKUP_ENDPOINT_URL, settings.BACKUP_REGION = "backups", "https://r2.example", "auto"
    settings.BACKUP_ACCESS_KEY_ID, settings.BACKUP_SECRET_ACCESS_KEY = "id", "secret"
    settings.BACKUP_PUBLIC_KEY, settings.BACKUP_DB_HEARTBEAT_URL = keys[0], ""
    return settings


def run(*args):
    out = StringIO()
    call_command("backup_database", *args, stdout=out)
    return out.getvalue()


def test_the_dump_is_a_real_database_with_our_data_in_it():
    Owner.objects.create(name="Distinct Owner Name", property_address="9 Backup Way")
    data, kind = dbbackup.dump_database()
    assert kind in ("postgres", "sqlite") and len(data) > 1000
    if kind == "sqlite":
        assert data.startswith(b"SQLite format 3\x00")
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.db"); open(p, "wb").write(data)
            assert sqlite3.connect(p).execute("select count(*) from violations_owner where name='Distinct Owner Name'").fetchone()[0] == 1
    else:
        assert data.startswith(b"PGDMP")


def test_encrypt_then_decrypt_with_the_private_key_gives_the_same_bytes(keys):
    data, _ = dbbackup.dump_database()
    blob = dbbackup.encrypt(data, keys[0])
    assert blob != data and data[:16] not in blob and dbbackup.decrypt(blob, keys[1]) == data


def test_a_different_private_key_cannot_open_it(keys):
    blob = dbbackup.encrypt(b"secret data" * 10, keys[0])
    other = f"{pyrage.x25519.Identity.generate()}\n"
    with pytest.raises(Exception):
        dbbackup.decrypt(blob, other)


@pytest.mark.skipif(not shutil.which("age"), reason="the age tool is not installed here")
def test_the_standard_age_tool_opens_what_we_wrote(keys, tmp_path):
    """So a restore never depends on our own code: `age -d -i key file` works."""
    data, _ = dbbackup.dump_database()
    (tmp_path / "b.age").write_bytes(dbbackup.encrypt(data, keys[0]))
    (tmp_path / "k.txt").write_text(keys[1])
    r = subprocess.run(["age", "-d", "-i", str(tmp_path / "k.txt"), str(tmp_path / "b.age")], capture_output=True)
    assert r.returncode == 0 and r.stdout == data


def test_a_bad_public_key_is_refused():
    with pytest.raises(dbbackup.BackupError, match="age public key"):
        dbbackup.encrypt(b"x", "not-a-key")


def test_storing_writes_a_dated_name_and_checks_the_read_back(keys):
    t = Mem()
    name, sha = dbbackup.store(t, b"blob", "sqlite")
    assert name.startswith("database/hoa-") and name.endswith(".sqlite3.age") and t.files[name] == b"blob"
    with pytest.raises(dbbackup.BackupError, match="read back differently"):
        dbbackup.store(Mem(corrupt=True), b"blob", "postgres")


def test_an_existing_name_is_never_replaced():
    from django.utils import timezone
    now = timezone.now()
    first = Mem()
    name, _ = dbbackup.store(first, b"one", "sqlite", now=now)
    with pytest.raises(dbbackup.BackupError, match="already exists"):
        dbbackup.store(first, b"two", "sqlite", now=now)
    assert first.files[name] == b"one"


def test_the_command_refuses_without_a_public_key(settings):
    settings.BACKUP_PUBLIC_KEY = ""
    with pytest.raises(CommandError, match="make_backup_key"):
        run()


def test_the_command_refuses_without_a_bucket(conf):
    conf.BACKUP_BUCKET = ""
    with pytest.raises(CommandError, match="not set up"):
        run()


def test_the_command_stores_only_an_encrypted_file_and_pings_the_heartbeat(conf, keys):
    Owner.objects.create(name="Findable Plain Text Name", property_address="1 A St")
    conf.BACKUP_DB_HEARTBEAT_URL = "https://monitor.example/ping/db"
    t = Mem()
    with mock.patch.object(backups, "S3Target", lambda *a, **k: t), mock.patch("urllib.request.urlopen") as ping:
        out = run()
    (name, blob), = t.files.items()
    assert "Backup complete." in out and name.startswith("database/") and name.endswith(".age") and ping.call_count == 1
    assert b"Findable Plain Text Name" not in blob and not blob.startswith(b"SQLite") and not blob.startswith(b"PGDMP")
    plain = dbbackup.decrypt(blob, keys[1])
    assert plain.startswith((b"SQLite format 3", b"PGDMP"))                 # opens to a real dump
    if plain.startswith(b"SQLite"):                                         # (a PostgreSQL dump compresses its data, so the name is not visible there)
        assert b"Findable Plain Text Name" in plain


def test_a_failed_store_means_no_heartbeat_and_an_error_exit(conf):
    conf.BACKUP_DB_HEARTBEAT_URL = "https://monitor.example/ping/db"
    with mock.patch.object(backups, "S3Target", lambda *a, **k: Mem(corrupt=True)), mock.patch("urllib.request.urlopen") as ping:
        with pytest.raises(CommandError, match="read back differently"):
            run()
    assert ping.call_count == 0


def test_a_dry_run_stores_and_pings_nothing(conf):
    conf.BACKUP_DB_HEARTBEAT_URL = "https://monitor.example/ping/db"
    t = Mem()
    with mock.patch.object(backups, "S3Target", lambda *a, **k: t), mock.patch("urllib.request.urlopen") as ping:
        out = run("--dry-run")
    assert "Dry run finished" in out and t.files == {} and ping.call_count == 0


def test_a_dump_that_fails_is_reported_as_a_failed_backup(conf):
    with mock.patch.object(dbbackup, "dump_database", side_effect=dbbackup.BackupError("pg_dump failed: nope")):
        with pytest.raises(CommandError, match="pg_dump failed"):
            run()


def test_a_dump_that_is_not_a_database_is_never_stored(conf):
    fake = mock.Mock(returncode=0, stdout=b"hello", stderr=b"")
    with mock.patch.object(dbbackup.connection, "settings_dict", {**connection.settings_dict, "ENGINE": "django.db.backends.postgresql"}), \
         mock.patch("subprocess.run", return_value=fake):
        with pytest.raises(dbbackup.BackupError, match="does not look like"):
            dbbackup.dump_database()


# --- key creation and restore helpers ---------------------------------------------------------------------------------------------
def test_make_backup_key_writes_the_private_key_to_a_file_and_never_shows_it(tmp_path):
    f = tmp_path / "k.txt"
    out = StringIO()
    call_command("make_backup_key", "--file", str(f), stdout=out)
    text = f.read_text()
    secret = next(x for x in text.splitlines() if x.startswith("AGE-SECRET-KEY-"))
    pub = next(x.split(": ", 1)[1] for x in text.splitlines() if x.startswith("# public key:"))
    assert secret not in out.getvalue() and pub in out.getvalue() and pub.startswith("age1")
    import os, stat
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    blob = dbbackup.encrypt(b"x" * 2000, pub)
    assert dbbackup.decrypt(blob, text) == b"x" * 2000                              # the pair really belongs together
    with pytest.raises(CommandError, match="already exists"):
        call_command("make_backup_key", "--file", str(f), stdout=StringIO())


def test_decrypt_backup_restores_the_file_and_refuses_to_overwrite(tmp_path, keys):
    data, _ = dbbackup.dump_database()
    (tmp_path / "b.age").write_bytes(dbbackup.encrypt(data, keys[0]))
    (tmp_path / "k.txt").write_text(keys[1])
    call_command("decrypt_backup", str(tmp_path / "b.age"), str(tmp_path / "k.txt"), str(tmp_path / "out.bin"), stdout=StringIO())
    assert (tmp_path / "out.bin").read_bytes() == data
    with pytest.raises(CommandError, match="already exists"):
        call_command("decrypt_backup", str(tmp_path / "b.age"), str(tmp_path / "k.txt"), str(tmp_path / "out.bin"))
    wrong = tmp_path / "wrong.txt"; wrong.write_text(f"{pyrage.x25519.Identity.generate()}\n")
    with pytest.raises(CommandError, match="Could not decrypt"):
        call_command("decrypt_backup", str(tmp_path / "b.age"), str(wrong), str(tmp_path / "out2.bin"))
    assert not (tmp_path / "out2.bin").exists()


def test_the_s3_target_can_check_whether_a_backup_name_exists():
    pytest.importorskip("boto3")
    from botocore.exceptions import ClientError
    from botocore.stub import Stubber
    t = backups.S3Target("backups", "https://r2.example", "auto", "id", "secret")
    with Stubber(t.client) as st:
        st.add_response("head_object", {}, {"Bucket": "backups", "Key": "a"})
        assert t.exists("a") is True
        st.add_client_error("head_object", service_error_code="404", http_status_code=404, expected_params={"Bucket": "backups", "Key": "b"})
        assert t.exists("b") is False
        st.add_client_error("head_object", service_error_code="403", http_status_code=403, expected_params={"Bucket": "backups", "Key": "c"})
        with pytest.raises(ClientError):
            t.exists("c")
