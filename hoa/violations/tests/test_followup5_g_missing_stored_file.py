"""Found by the real-browser check on 2026-11-25: a photo whose file is no longer in storage (lost, moved, a restore without the media files) answered with a
server error (HTTP 500) and a stack trace in the log. It now answers 404 "not found" like any other photo that is not there, and the case page still opens."""
import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from violations.models import Photo
from violations.tests.review4_support import board, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def test_F5_a_photo_whose_file_is_gone_is_a_404_not_a_server_error(board):
    v = make_case()
    p = Photo.objects.create(violation=v, from_resident=False)
    p.image.name = "photos/never-stored.jpg"          # the row exists, the file does not
    p.save()
    r = board.get(reverse("photo", args=[p.pk]))
    assert r.status_code == 404
    assert board.get(reverse("violation_detail", args=[v.pk])).status_code == 200      # the case page itself still opens


def test_F5_a_photo_that_is_there_still_opens(board):
    v = make_case()
    p = Photo(violation=v, from_resident=False)
    p.image.save("x.png", ContentFile(b"\x89PNG\r\n\x1a\n" + b"0" * 20), save=True)
    r = board.get(reverse("photo", args=[p.pk]))
    assert r.status_code == 200 and r["Content-Type"] == "image/png"
