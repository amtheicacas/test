"""Review pass 4 (R4-20): hostile picture uploads, on every way in (staff, a signed-in owner, an owner's private link, a committee site visit). A picture is accepted only if it REALLY is a
picture (what the file is, never what its name or declared type says); the stored name is made up by the server; and a stored file can only ever be shown as a picture (fixed type, no sniffing, a
policy that forbids running anything). Nothing is sent anywhere."""
import io
import struct
import zlib
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image

from violations import uploads
from violations.models import Photo, Violation
from violations.tests.review4_support import board, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status


def jpeg(size=(40, 30)):
    b = io.BytesIO()
    Image.new("RGB", size, "red").save(b, "JPEG")
    return b.getvalue()


def png_claiming(width, height):
    """A tiny file whose header claims a huge picture (a 'decompression bomb')."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")


HOSTILE = {
    "html_named_jpg": (b"<html><body><script>alert(document.cookie)</script></body></html>", "a.jpg", "image/jpeg"),
    "svg_with_script": (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "a.svg", "image/svg+xml"),
    "svg_named_png": (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "a.png", "image/png"),
    "php_named_jpg": (b"<?php system($_GET['c']); ?>", "shell.php.jpg", "image/jpeg"),
    "pdf_named_jpg": (b"%PDF-1.4\n1 0 obj<</JS (app.alert(1))>>endobj", "a.jpg", "image/jpeg"),
    "empty": (b"", "a.jpg", "image/jpeg"),
    "truncated_jpeg": (jpeg()[:60], "a.jpg", "image/jpeg"),
    "png_bomb": (png_claiming(100_000, 100_000), "a.png", "image/png"),
    "text_named_gif": (b"GIF89a but not really", "a.gif", "image/gif"),
}


@pytest.mark.parametrize("name", HOSTILE)
def test_R4_20_something_that_is_not_a_real_picture_is_never_accepted(name):
    data, filename, ctype = HOSTILE[name]
    f = SimpleUploadedFile(filename, data, content_type=ctype)
    assert uploads.image_ext(f) is None


@pytest.mark.parametrize("name", HOSTILE)
def test_R4_20_staff_adding_photos_to_a_case_keeps_nothing_hostile(board, name):
    data, filename, ctype = HOSTILE[name]
    v = make_case(S.NOTICE_SENT)
    r = board.post(reverse("violation_action", args=[v.pk, "add_photos"]), {"photos": SimpleUploadedFile(filename, data, content_type=ctype)}, follow=True)
    assert r.status_code == 200 and Photo.objects.count() == 0
    assert "not added" in r.content.decode()                                         # and the person is told


@pytest.mark.parametrize("name", HOSTILE)
def test_R4_20_an_owner_with_only_the_private_link_cannot_upload_anything_hostile(name):
    data, filename, ctype = HOSTILE[name]
    v = make_case(S.NOTICE_SENT)
    r = Client().post(reverse("resident", args=[v.token]), {"message": "here is my photo", "photos": SimpleUploadedFile(filename, data, content_type=ctype)}, follow=True)
    assert r.status_code == 200 and Photo.objects.count() == 0


def test_R4_20_a_real_picture_with_script_text_stuck_on_the_end_is_stored_under_a_made_up_name_and_only_ever_served_as_a_picture(board):
    v = make_case(S.NOTICE_SENT)
    evil = jpeg() + b"<script>alert(1)</script>"                                      # a real JPEG, then HTML: valid as a picture, harmless only if it is never shown as a page
    board.post(reverse("violation_action", args=[v.pk, "add_photos"]), {"photos": SimpleUploadedFile("../../evil<script>.html.jpg", evil, content_type="text/html")})
    p = Photo.objects.get()
    assert p.image.name.startswith("photos/") and p.image.name.endswith(".jpg") and ".." not in p.image.name and "evil" not in p.image.name and "<" not in p.image.name
    r = board.get(reverse("photo", args=[p.pk]))
    assert r["Content-Type"] == "image/jpeg" and r["X-Content-Type-Options"] == "nosniff"
    assert r["Content-Security-Policy"] == "default-src 'none'; sandbox" and "attachment" not in r.get("Content-Disposition", "")


def test_R4_20_a_photo_of_one_case_is_not_shown_for_another_cases_private_link():
    a, b = make_case(S.NOTICE_SENT, n=1), make_case(S.NOTICE_SENT, n=2)
    from django.core.files.base import ContentFile
    p = Photo.objects.create(violation=a, image=ContentFile(jpeg(), name="p.jpg"))
    c = Client()
    assert c.get(reverse("resident_photo", args=[a.token, p.pk])).status_code == 200
    assert c.get(reverse("resident_photo", args=[b.token, p.pk])).status_code == 404                  # (somebody else's link)
