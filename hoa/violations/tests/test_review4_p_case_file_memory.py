"""Review pass 4 (R4-16): the case-file download (and the attorney package) used to build the whole zip in memory, several times over: 40 photos of 9 MB needed
more than 700 MB, which would stop a small host (an owner can add 30 photos to a case through their private link). The zip is now written to a temporary file one file at a time and
sent from there. Nothing is sent anywhere."""
import csv
import hashlib
import io
import os
import tempfile
import tracemalloc
import zipfile
from datetime import date, timedelta

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from violations import casefile
from violations.models import Photo, Violation
from violations.tests.review4_support import board, body, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status


def case_with_photos(count, size):
    v = make_case(S.NOTICE_SENT)
    for i in range(count):
        Photo.objects.create(violation=v, from_resident=i % 3 == 0, image=ContentFile(os.urandom(size), name=f"p{i}.jpg"))
    return v


def test_R4_16_a_case_with_many_large_photos_is_sent_without_holding_the_whole_zip_in_memory(board):
    v = case_with_photos(12, 5 * 1024 * 1024)                                  # about 60 MB of photos that do not compress
    tracemalloc.start()
    r = board.get(reverse("case_file", args=[v.pk]))
    total = sum(len(chunk) for chunk in r.streaming_content) if getattr(r, "streaming", False) else len(r.content)         # (read the way a browser does)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert r.status_code == 200 and total > 55 * 1024 * 1024
    assert peak < total / 2, f"the download needed {peak / 1e6:.0f} MB of memory for a {total / 1e6:.0f} MB zip"


def test_R4_16_the_download_still_carries_the_same_headers(board):
    v = case_with_photos(1, 2000)
    r = board.get(reverse("case_file", args=[v.pk]))
    assert r["Content-Type"] == "application/zip" and r["Cache-Control"] == "no-store"
    assert r["Content-Disposition"] == f'attachment; filename="case-{v.pk}-1-Review-Way.zip"'


def test_R4_16_the_manifest_lists_every_other_file_with_its_true_size_and_fingerprint():
    v = case_with_photos(4, 30_000)
    data, name = casefile.build_case_file(v, None)
    z = zipfile.ZipFile(io.BytesIO(data))
    inside = {n.split("/", 1)[1]: z.read(n) for n in z.namelist()}
    rows = list(csv.DictReader(io.StringIO(inside.pop("manifest.csv").decode("utf-8-sig"))))
    assert sorted(r["File"] for r in rows) == sorted(inside)                              # every file but the manifest itself, once each
    for r in rows:
        assert int(r["Bytes"]) == len(inside[r["File"]]) and r["SHA-256 fingerprint"] == hashlib.sha256(inside[r["File"]]).hexdigest()
    assert name == f"case-{v.pk}-1-Review-Way.zip" and len([n for n in inside if n.startswith("photos/photo-")]) == 4


def test_R4_16_writing_to_a_file_and_building_in_memory_give_the_same_zip():
    v = case_with_photos(3, 20_000)
    in_memory, name = casefile.build_case_file(v, None)
    with tempfile.TemporaryFile() as tmp:
        assert casefile.build_case_file_to(tmp, v, None) == name
        tmp.seek(0)
        on_disk = zipfile.ZipFile(tmp)
        a = {n: on_disk.read(n) for n in on_disk.namelist()}
    b = {n: zipfile.ZipFile(io.BytesIO(in_memory)).read(n) for n in zipfile.ZipFile(io.BytesIO(in_memory)).namelist()}
    assert sorted(a) == sorted(b)
    same = [n for n in a if not n.endswith(("00-INDEX.html", "manifest.csv"))]             # (the index says when it was prepared)
    assert same and all(a[n] == b[n] for n in same)
