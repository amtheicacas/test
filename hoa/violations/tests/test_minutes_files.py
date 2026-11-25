"""Uploaded committee minutes (SEC-15): PDF and Word .docx only, judged by content, random stored name, add-only history, permission-checked download."""
import hashlib
import io
import zipfile
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from violations import minutesfiles
from violations.models import CommitteeMeeting, CommitteeMember, MinutesFile, Owner, Profile

pytestmark = pytest.mark.django_db
U = get_user_model()


def make_user(name, role, owner=None):
    u = U.objects.create_user(name, f"{name}@example.com", "x", first_name=name.title(), last_name="Tester")
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


def pdf_bytes(text="Minutes"):
    from reportlab.pdfgen import canvas
    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.drawString(72, 720, text)
    c.save()
    return b.getvalue()


def docx_bytes(extra=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return b.getvalue()


def body(r):
    return b"".join(r.streaming_content)


def up(name, data, ctype="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=ctype)


@pytest.fixture
def w():
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=2))
    oa = Owner.objects.create(name="Alex Owner", property_address="1 Case Way", email="a@example.com")
    year = timezone.localdate().year
    out = dict(m=m, oa=oa)
    out["board"], out["manager"] = make_user("board1", "board"), make_user("mgr1", "manager")
    out["committee"] = make_user("comm1", "committee")
    CommitteeMember.objects.create(name="Comm One", year=year, user=out["committee"])
    out["home"] = make_user("homea", "homeowner", oa)
    return type("W", (), out)


def save(client, user, m, data):
    client.force_login(user)
    return client.post(f"/committee/meetings/{m.pk}/save/", {"attendees": "x", **({"minutes": data} if data is not None else {})})


# --- what is accepted ----------------------------------------------------------------------------------------------------------------
def test_a_real_pdf_is_accepted_and_recorded_with_who_when_and_fingerprint(client, w):
    data = pdf_bytes()
    save(client, w.committee, w.m, up("Minutes Nov.pdf", data))
    f = MinutesFile.objects.get()
    assert f.kind == "pdf" and f.size == len(data) and f.sha256 == hashlib.sha256(data).hexdigest()
    assert f.original_name == "Minutes Nov.pdf" and f.uploaded_by == w.committee and f.uploaded_by_name == "Comm1 Tester"
    assert abs(f.uploaded_at - timezone.now()) < timedelta(minutes=1) and f.replaces is None


def test_a_real_docx_is_accepted(client, w):
    save(client, w.board, w.m, up("minutes.docx", docx_bytes()))
    assert MinutesFile.objects.get().kind == "docx"


def test_stored_under_a_random_name_never_the_uploaders(client, w):
    save(client, w.board, w.m, up("../../evil name?.pdf", pdf_bytes()))
    f = MinutesFile.objects.get()
    assert "evil" not in f.file.name and f.file.name.startswith("minutes/") and f.file.name.endswith(".pdf")
    assert "/" not in f.original_name and "\\" not in f.original_name
    save(client, w.board, w.m, up("evil.pdf", pdf_bytes("two")))
    a, b = MinutesFile.objects.order_by("pk")
    assert a.file.name != b.file.name                                                   # every upload gets its own random name


@pytest.mark.parametrize("name,data", [
    ("minutes.txt", b"just text"),
    ("minutes.png", b"\x89PNG\r\n\x1a\n" + b"0" * 50),
    ("minutes.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 600),                   # old Word: refused
    ("minutes.html", b"<script>alert(1)</script>"),
    ("fake.pdf", b"<html><script>alert(1)</script></html>"),                             # right extension, wrong content
    ("fake.docx", b"not a zip at all"),
    ("empty.pdf", b""),
    ("minutes.exe", b"MZ" + b"0" * 200),
])
def test_anything_else_is_refused_and_nothing_is_stored(client, w, name, data):
    r = save(client, w.committee, w.m, up(name, data))
    assert r.status_code == 302 and not MinutesFile.objects.exists()
    w.m.refresh_from_db(); assert not w.m.minutes
    page = client.get(f"/committee/meetings/{w.m.pk}/").content.decode()
    assert "Meeting details" in page


def test_the_refusal_is_explained(client, w):
    client.force_login(w.committee)
    r = client.post(f"/committee/meetings/{w.m.pk}/save/", {"attendees": "x", "minutes": up("a.txt", b"hi")}, follow=True)
    assert "PDF or a Word (.docx)" in r.content.decode().replace("&amp;", "&")


def test_a_pdf_name_on_a_word_file_is_judged_by_content(client, w):
    save(client, w.board, w.m, up("really-word.pdf", docx_bytes()))
    assert MinutesFile.objects.get().kind == "docx"              # stored by what it is, not by what it was called


def test_word_files_with_macros_or_embedded_programs_are_refused(client, w):
    for extra in ({"word/vbaProject.bin": b"x"}, {"word/embeddings/oleObject1.bin": b"x"}):
        save(client, w.board, w.m, up("m.docx", docx_bytes(extra)))
    assert not MinutesFile.objects.exists()


def test_a_zip_that_unpacks_to_something_huge_is_refused(client, w):
    big = docx_bytes({"word/media/x.bin": b"0" * (101 * 1024 * 1024)})
    assert len(big) < minutesfiles.MAX_BYTES                      # tiny to upload, enormous to unpack
    save(client, w.board, w.m, up("m.docx", big))
    assert not MinutesFile.objects.exists()


def test_a_pdf_with_a_script_or_launcher_is_refused(client, w):
    for marker in (b"/JavaScript", b"/Launch"):
        save(client, w.board, w.m, up("m.pdf", pdf_bytes() + b"\n% " + marker))
    assert not MinutesFile.objects.exists()


def test_a_pdf_header_with_garbage_after_it_is_refused(client, w):
    save(client, w.board, w.m, up("m.pdf", b"%PDF-1.4\n" + b"garbage" * 100))
    assert not MinutesFile.objects.exists()


def test_over_ten_megabytes_is_refused(client, w):
    save(client, w.board, w.m, up("m.pdf", pdf_bytes() + b"0" * (10 * 1024 * 1024)))
    assert not MinutesFile.objects.exists()


# --- history: replacing never deletes ------------------------------------------------------------------------------------------------
def test_uploading_again_adds_a_new_current_file_and_keeps_the_old_one(client, w):
    one, two = pdf_bytes("first"), pdf_bytes("second")
    save(client, w.committee, w.m, up("one.pdf", one))
    save(client, w.board, w.m, up("two.pdf", two))
    a, b = MinutesFile.objects.order_by("pk")
    assert b.replaces == a and a.replaces is None and MinutesFile.objects.count() == 2
    assert a.read_bytes() == one and b.read_bytes() == two and a.file.name != b.file.name       # the first file is still on disk, untouched
    assert minutesfiles.current(w.m) == b
    assert body(client.get(f"/committee/meetings/{w.m.pk}/minutes/")) == two                    # current = newest
    assert body(client.get(f"/committee/meetings/{w.m.pk}/minutes/?v={a.pk}")) == one         # the earlier one stays reachable
    page = client.get(f"/committee/meetings/{w.m.pk}/").content.decode()
    assert "Earlier uploads (1" in page and "one.pdf" in page and "two.pdf" in page


def test_a_save_without_a_file_changes_nothing_in_the_history(client, w):
    save(client, w.board, w.m, up("one.pdf", pdf_bytes()))
    save(client, w.board, w.m, None)
    assert MinutesFile.objects.count() == 1


def test_records_can_never_be_changed_or_deleted(client, w):
    save(client, w.board, w.m, up("one.pdf", pdf_bytes()))
    f = MinutesFile.objects.get()
    f.original_name = "changed"
    with pytest.raises(ValidationError):
        f.save()
    with pytest.raises(PermissionDenied):
        f.delete()
    f.refresh_from_db(); assert f.original_name == "one.pdf"


def test_the_admin_is_look_only(client, w):
    from django.contrib import admin
    ma = admin.site._registry[MinutesFile]
    assert not ma.has_add_permission(None) and not ma.has_change_permission(None) and not ma.has_delete_permission(None)


def test_approved_minutes_lock_the_upload_too(client, w):
    w.m.minutes_status = "approved"; w.m.save()
    save(client, w.board, w.m, up("late.pdf", pdf_bytes()))
    assert not MinutesFile.objects.exists()


# --- who may download, and how --------------------------------------------------------------------------------------------------------
def test_committee_board_and_manager_can_download_and_an_owner_cannot(client, w):
    data = pdf_bytes("private")
    save(client, w.board, w.m, up("m.pdf", data))
    url = f"/committee/meetings/{w.m.pk}/minutes/"
    for u in (w.committee, w.board, w.manager):
        client.force_login(u)
        assert body(client.get(url)) == data
    client.force_login(w.home)
    assert client.get(url).status_code in (302, 403) and data not in client.get(url).content
    client.logout()
    r = client.get(url)
    assert r.status_code == 302 and "login" in r["Location"]
    for vid in (f"?v={MinutesFile.objects.get().pk}",):
        client.force_login(w.home)
        assert client.get(url + vid).status_code in (302, 403)


def test_the_download_is_a_download_with_a_fixed_type_and_safe_headers(client, w):
    save(client, w.board, w.m, up("<script>.pdf", pdf_bytes()))
    r = client.get(f"/committee/meetings/{w.m.pk}/minutes/")
    assert r["Content-Type"] == "application/pdf" and r["X-Content-Type-Options"] == "nosniff"
    assert r["Content-Disposition"].startswith("attachment") and "<script>" not in r["Content-Disposition"] and "committee-minutes-" in r["Content-Disposition"]
    assert "no-store" in r["Cache-Control"] and "sandbox" in r["Content-Security-Policy"]
    save(client, w.board, w.m, up("w.docx", docx_bytes()))
    assert "wordprocessingml" in client.get(f"/committee/meetings/{w.m.pk}/minutes/")["Content-Type"]


def test_a_changed_file_is_not_sent(client, w):
    save(client, w.board, w.m, up("m.pdf", pdf_bytes()))
    f = MinutesFile.objects.get()
    with f.file.storage.open(f.file.name, "wb") as fh:
        fh.write(b"tampered")
    r = client.get(f"/committee/meetings/{w.m.pk}/minutes/")
    assert r.status_code == 409 and b"tampered" not in r.content


def test_a_missing_file_is_a_clean_404(client, w):
    save(client, w.board, w.m, up("m.pdf", pdf_bytes()))
    f = MinutesFile.objects.get()
    f.file.storage.delete(f.file.name)
    assert client.get(f"/committee/meetings/{w.m.pk}/minutes/").status_code == 404


def test_an_unknown_or_foreign_version_is_a_404(client, w):
    other = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=9))
    save(client, w.board, other, up("o.pdf", pdf_bytes("other")))
    theirs = MinutesFile.objects.get()
    save(client, w.board, w.m, up("m.pdf", pdf_bytes("mine")))
    base = f"/committee/meetings/{w.m.pk}/minutes/"
    assert client.get(base + f"?v={theirs.pk}").status_code == 404 and client.get(base + "?v=abc").status_code == 404
    assert client.get(base + "?v=99999").status_code == 404


def test_no_file_is_a_404(client, w):
    client.force_login(w.board)
    assert client.get(f"/committee/meetings/{w.m.pk}/minutes/").status_code == 404


def test_a_file_uploaded_before_the_history_existed_is_still_offered_safely(client, w):
    from django.core.files.base import ContentFile
    w.m.minutes.save("old.html", ContentFile(b"<script>alert(1)</script>"))
    client.force_login(w.board)
    r = client.get(f"/committee/meetings/{w.m.pk}/minutes/")
    assert r["Content-Type"] == "application/octet-stream" and r["Content-Disposition"].startswith("attachment")
    assert "uploaded before upload history" in client.get(f"/committee/meetings/{w.m.pk}/").content.decode()


def test_storage_is_used_without_a_disk_path(client, w, settings):
    save(client, w.board, w.m, up("m.pdf", pdf_bytes()))
    from unittest import mock
    from django.core.files.storage import default_storage
    with mock.patch.object(type(default_storage), "path", side_effect=NotImplementedError("no disk here"), create=True):
        assert client.get(f"/committee/meetings/{w.m.pk}/minutes/").status_code == 200


def test_the_written_minutes_are_unchanged_by_uploads(client, w):
    from violations import minutes
    before = minutes.build(w.m)
    save(client, w.board, w.m, up("m.pdf", pdf_bytes()))
    assert minutes.build(w.m) == before
