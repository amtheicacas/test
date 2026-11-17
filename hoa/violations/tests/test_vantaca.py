import json
from unittest import mock

import pytest

from violations import vantaca
from violations.models import Owner

pytestmark = pytest.mark.django_db

CSV = "Account ID,Homeowner Name,Property Address,Email Address,Mailing Address\n" \
      "A1,Pat Owner,12 Palm Way,pat@example.com,PO Box 9\nA2,,3 Oak Ln,,\n"


def test_csv_import_creates_and_skips():
    assert vantaca.sync_csv(CSV) == {"created": 1, "updated": 0, "skipped": 1}
    o = Owner.objects.get()
    assert (o.external_id, o.email, o.mailing_address) == ("A1", "pat@example.com", "PO Box 9")


def test_resync_updates_and_preserves_notes():
    vantaca.sync_csv(CSV)
    Owner.objects.update(notes="called twice")
    vantaca.sync_csv(CSV.replace("pat@example.com", "new@example.com"))
    o = Owner.objects.get()
    assert o.email == "new@example.com" and o.notes == "called twice"


def test_links_hand_entered_owner_by_address():
    Owner.objects.create(name="Old", property_address="12 palm way")
    vantaca.sync_csv(CSV)
    assert Owner.objects.count() == 1 and Owner.objects.get().external_id == "A1"


def test_api_fetch_handles_wrapper_and_first_last(settings):
    settings.VANTACA_BASE_URL, settings.VANTACA_OWNERS_PATH = "https://api.example", "read/owners"
    settings.VANTACA_USER, settings.VANTACA_PASSWORD = "u", "p"
    payload = {"items": [{"AccountId": 7, "FirstName": "Sam", "LastName": "Lee", "Address": "5 Bay St"}]}
    resp = mock.MagicMock()
    resp.__enter__.return_value = mock.MagicMock(read=lambda *_: json.dumps(payload).encode())
    with mock.patch("urllib.request.urlopen", return_value=resp) as m:
        assert vantaca.sync_api()["created"] == 1
    assert m.call_args[0][0].full_url == "https://api.example/read/owners"
    assert Owner.objects.get().name == "Sam Lee"


def test_api_requires_config(settings):
    settings.VANTACA_BASE_URL = ""
    with pytest.raises(RuntimeError):
        vantaca.sync_api()
