"""The file backup: copy only, verified, never overwrites or deletes, and checks the database's files are all in the backup."""
import hashlib
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from io import StringIO

from violations import backups
from violations.models import CommitteeMeeting, MinutesFile, Notice, Owner, SentLetter, Violation, ViolationType

pytestmark = pytest.mark.django_db


class Mem:
    """A stand-in for the backup bucket."""
    def __init__(self, files=None, corrupt=False, fail_put=()):
        self.files = dict(files or {}); self.meta = {}; self.corrupt, self.fail_put = corrupt, set(fail_put)
        self.puts, self.deletes = [], []

    def existing(self):
        return {k: len(v) for k, v in self.files.items()}

    def put(self, key, data, sha):
        if key in self.fail_put:
            raise OSError("bucket said no")
        self.puts.append(key); self.files[key] = data + (b"x" if self.corrupt else b""); self.meta[key] = sha

    def get(self, key):
        return self.files[key]


def store(name, data=b"hello"):
    return default_storage.save(name, ContentFile(data))


def test_copies_every_file_in_every_folder_and_checks_the_copy(settings):
    a, b = store("photos/a.jpg", b"AAA"), store("sent-letters/2026/b.pdf", b"BBBB")
    t = Mem()
    rep = backups.copy_files(default_storage, t)
    assert sorted(rep.copied) == sorted([a, b]) and rep.ok and rep.bytes_copied == 7
    assert t.files[a] == b"AAA" and t.files[b] == b"BBBB" and t.meta[a] == hashlib.sha256(b"AAA").hexdigest()


def test_a_second_run_copies_nothing_and_never_touches_what_is_there():
    a = store("photos/a.jpg", b"AAA")
    t = Mem()
    backups.copy_files(default_storage, t)
    t.puts.clear()
    rep = backups.copy_files(default_storage, t)
    assert rep.copied == [] and rep.present == [a] and t.puts == [] and rep.ok


def test_only_new_files_are_copied_later():
    a = store("photos/a.jpg", b"AAA")
    t = Mem()
    backups.copy_files(default_storage, t)
    b = store("photos/b.jpg", b"BBB")
    rep = backups.copy_files(default_storage, t)
    assert rep.copied == [b] and rep.present == [a]


def test_a_different_file_under_the_same_name_is_a_conflict_and_is_left_alone():
    a = store("photos/a.jpg", b"original")
    t = Mem({a: b"someone else"})
    rep = backups.copy_files(default_storage, t)
    assert rep.conflicts == [a] and not rep.ok and t.files[a] == b"someone else" and t.puts == []


def test_same_size_but_different_content_is_caught_by_verify():
    a = store("photos/a.jpg", b"AAAA")
    t = Mem({a: b"BBBB"})
    assert backups.copy_files(default_storage, t).conflicts == []             # size alone says "present"
    assert backups.copy_files(default_storage, t, verify=True).conflicts == [a]
    assert t.files[a] == b"BBBB"


def test_nothing_in_the_backup_is_ever_deleted():
    a = store("photos/a.jpg", b"A")
    t = Mem({"photos/old-and-gone-from-live.jpg": b"keep me"})
    backups.copy_files(default_storage, t)
    assert t.files["photos/old-and-gone-from-live.jpg"] == b"keep me" and t.deletes == [] and not hasattr(t, "delete")


def test_a_copy_that_reads_back_differently_is_a_failure_not_a_success():
    store("photos/a.jpg", b"AAA")
    rep = backups.copy_files(default_storage, Mem(corrupt=True))
    assert rep.copied == [] and rep.failures and "differently" in rep.failures[0][1] and not rep.ok


def test_one_bad_file_does_not_stop_the_others():
    a, b = store("photos/a.jpg", b"A"), store("photos/b.jpg", b"B")
    rep = backups.copy_files(default_storage, Mem(fail_put=[a]))
    assert rep.copied == [b] and [n for n, _ in rep.failures] == [a] and not rep.ok


def test_a_dry_run_writes_nothing():
    a = store("photos/a.jpg", b"A")
    t = Mem()
    rep = backups.copy_files(default_storage, t, dry_run=True)
    assert rep.would_copy == [a] and t.puts == [] and t.files == {}


def test_the_storage_self_test_files_are_not_copied():
    store("storage-check/x.txt", b"junk")
    t = Mem()
    rep = backups.copy_files(default_storage, t)
    assert t.files == {} and rep.skipped == ["storage-check/x.txt"]


def test_files_the_database_points_at_must_be_in_the_backup(settings):
    m = CommitteeMeeting.objects.create(starts_at=timezone.now())
    inb = MinutesFile(meeting=m, kind="pdf", sha256="0" * 64)
    inb.file.save("m.pdf", ContentFile(b"%PDF-1.4"), save=False); inb.save()          # add-only records are written once, in one go
    o = Owner.objects.create(name="Pat", property_address="1 A St")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T"), description="d", cure_required="c")
    n = Notice.objects.create(violation=v, kind="violation")
    sl = SentLetter(violation=v, notice=n, kind="violation", sha256="1" * 64)
    sl.file.save("l.pdf", ContentFile(b"%PDF-1.4 letter"), save=False); sl.save()
    t = Mem()
    rep = backups.copy_files(default_storage, t)
    assert backups.check_database(rep, t).missing_from_backup == [] and rep.ok
    t.files.pop(sl.file.name)                                       # the backup loses a letter
    rep2 = backups.check_database(backups.Report(), t)
    assert rep2.missing_from_backup == [sl.file.name] and not rep2.ok


@pytest.fixture
def conf(settings):
    settings.BACKUP_BUCKET, settings.BACKUP_ENDPOINT_URL = "backups", "https://r2.example"
    settings.BACKUP_ACCESS_KEY_ID, settings.BACKUP_SECRET_ACCESS_KEY, settings.BACKUP_REGION = "id", "secret", "auto"
    settings.BACKUP_HEARTBEAT_URL = ""
    return settings


def run(*args):
    out = StringIO()
    call_command("backup_files", *args, stdout=out)
    return out.getvalue()


def test_the_command_refuses_without_a_backup_bucket(settings):
    settings.BACKUP_BUCKET = ""
    with pytest.raises(CommandError, match="not set up"):
        run()


def test_the_command_refuses_a_backup_that_is_the_live_bucket(conf):
    conf.STORAGES = {"default": {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {"bucket_name": "backups", "endpoint_url": "https://r2.example"}},
                     "staticfiles": conf.STORAGES["staticfiles"]}
    with pytest.raises(CommandError, match="same as the live bucket"):
        run()


def test_the_command_reports_and_exits_with_an_error_when_incomplete(conf):
    a = store("photos/a.jpg", b"A")
    t = Mem({a: b"different"})
    with mock.patch.object(backups, "S3Target", lambda *a, **k: t):
        with pytest.raises(CommandError, match="NOT complete"):
            run()


def test_the_command_succeeds_and_pings_the_heartbeat_only_when_complete(conf):
    conf.BACKUP_HEARTBEAT_URL = "https://monitor.example/ping/abc"
    store("photos/a.jpg", b"A")
    t = Mem()
    with mock.patch.object(backups, "S3Target", lambda *a, **k: t), mock.patch("urllib.request.urlopen") as ping:
        out = run()
    assert "Backup complete." in out and "copied now:            1" in out and ping.call_count == 1
    t2 = Mem({"photos/a.jpg": b"zzzzzz"})
    with mock.patch.object(backups, "S3Target", lambda *a, **k: t2), mock.patch("urllib.request.urlopen") as ping:
        with pytest.raises(CommandError):
            run()
    assert ping.call_count == 0


def test_a_dry_run_through_the_command_changes_nothing_and_does_not_ping(conf):
    conf.BACKUP_HEARTBEAT_URL = "https://monitor.example/ping/abc"
    store("photos/a.jpg", b"A")
    t = Mem()
    with mock.patch.object(backups, "S3Target", lambda *a, **k: t), mock.patch("urllib.request.urlopen") as ping:
        out = run("--dry-run")
    assert "would copy:            1" in out and t.files == {} and ping.call_count == 0


def test_an_unreachable_heartbeat_does_not_fail_the_backup(conf):
    conf.BACKUP_HEARTBEAT_URL = "https://monitor.example/ping/abc"
    store("photos/a.jpg", b"A")
    with mock.patch.object(backups, "S3Target", lambda *a, **k: Mem()), mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert "heartbeat monitor could not be reached" in run()


def test_the_bucket_client_lists_all_pages_writes_with_a_fingerprint_and_reads_back():
    pytest.importorskip("boto3")                                        # only installed where a bucket is used (requirements-production.txt)
    """The real bucket wrapper, driven by recorded replies instead of a network."""
    import io as _io
    from botocore.stub import Stubber
    t = backups.S3Target("backups", "https://r2.example", "auto", "id", "secret")
    with Stubber(t.client) as st:
        st.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "n", "Contents": [{"Key": "a", "Size": 1}]},
                        {"Bucket": "backups"})
        st.add_response("list_objects_v2", {"IsTruncated": False, "Contents": [{"Key": "b", "Size": 2}]},
                        {"Bucket": "backups", "ContinuationToken": "n"})
        assert t.existing() == {"a": 1, "b": 2}
        st.add_response("put_object", {}, {"Bucket": "backups", "Key": "k", "Body": b"data", "Metadata": {"sha256": "abc"}})
        t.put("k", b"data", "abc")
        from botocore.response import StreamingBody
        st.add_response("get_object", {"Body": StreamingBody(_io.BytesIO(b"data"), 4)}, {"Bucket": "backups", "Key": "k"})
        assert t.get("k") == b"data"
        st.assert_no_pending_responses()
    assert not hasattr(t, "delete") and not hasattr(t, "delete_object")           # the backup code has no way to delete


def test_backblazes_empty_folder_placeholder_is_not_copied():
    store("photos/.bzEmpty", b"")
    real = store("photos/a.jpg", b"A")
    t = Mem()
    rep = backups.copy_files(default_storage, t)
    assert rep.copied == [real] and "photos/.bzEmpty" not in t.files and "photos/.bzEmpty" in rep.skipped
