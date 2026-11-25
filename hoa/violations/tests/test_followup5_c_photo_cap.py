"""Follow-up after review pass 4 (owner's answer: cap photos to 4): an owner (or anyone holding the private link) can add at most 4 photos to one case
(it was 30). The Association's own photos are not counted. Every test here FAILED on the code before the change. Nothing is sent anywhere."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from violations import views
from violations.models import Photo
from violations.tests.test_phase2_security import case, picture, post_private

pytestmark = pytest.mark.django_db


def test_F5_the_limit_is_four():
    assert views.MAX_RESIDENT_PHOTOS_PER_CASE == 4


def test_F5_six_photos_in_one_go_keep_four_and_say_why(client):
    v = case()
    r = post_private(client, v, [picture(name=f"{i}.png") for i in range(6)])
    page = client.get(r["Location"]).content.decode() if r.status_code == 302 else r.content.decode()
    assert Photo.objects.filter(violation=v, from_resident=True).count() == 4
    assert "most photos" in page and "4" in page


def test_F5_a_later_upload_after_four_adds_nothing(client):
    v = case()
    post_private(client, v, [picture(name=f"{i}.png") for i in range(3)])
    post_private(client, v, [picture(name="a.png"), picture(name="b.png")])
    post_private(client, v, [picture(name="c.png")])
    assert Photo.objects.filter(violation=v, from_resident=True).count() == 4


def test_F5_the_associations_own_photos_do_not_use_up_the_four(client):
    v = case()
    Photo.objects.create(violation=v, from_resident=False, image=SimpleUploadedFile("x.png", picture().read(), content_type="image/png"))
    post_private(client, v, [picture(name=f"{i}.png") for i in range(4)])
    assert Photo.objects.filter(violation=v, from_resident=True).count() == 4
