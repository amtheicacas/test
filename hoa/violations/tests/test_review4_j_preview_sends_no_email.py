"""Review pass 4 (R4-10): Looking at the import PREVIEW emailed owners 'your sign-in email was changed' although nothing had been imported.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date

from unittest import mock

import pytest
from django.core import mail

from violations import vantaca
from violations.models import (Owner, Profile, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


def _owner_with_login(email="old@example.com"):
    o = Owner.objects.create(name="Pat Prev", property_address="9 Preview Way", email=email, external_id="H9", city="Sarasota", state="FL", zip_code="34240")
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.create_user(email, email, "pw-for-tests-123")
    Profile.objects.create(user=u, role="homeowner", owner=o, relationship="owner")
    mail.outbox.clear()
    return o, u


ROW = {"Homeowner ID": "H9", "Homeowner(s)": "Pat Prev", "Address": "9 Preview Way", "Zip": "34240", "Email": "new@example.com"}


def test_R4_10_the_import_preview_sends_no_email_and_changes_nothing():
    o, u = _owner_with_login()
    vantaca.preview_rows([dict(ROW)])
    assert mail.outbox == []
    o.refresh_from_db()
    u.refresh_from_db()
    assert o.email == "old@example.com" and u.username == "old@example.com"


def test_R4_10_the_real_import_still_tells_the_old_address_once(django_capture_on_commit_callbacks):
    o, u = _owner_with_login()
    with django_capture_on_commit_callbacks(execute=True):          # (each row is saved in its own transaction; the notice goes out when it commits)
        vantaca.sync_rows([dict(ROW)])
    assert [m.to for m in mail.outbox] == [["old@example.com"]]
    u.refresh_from_db()
    assert u.username == "new@example.com"


def test_R4_10_a_row_that_fails_after_the_login_change_is_undone_and_nobody_is_told():
    o, u = _owner_with_login()
    with mock.patch("violations.accounts.invite_owner", side_effect=RuntimeError("boom")):       # the row fails AFTER its login email was changed
        result = vantaca.sync_rows([dict(ROW)], invite=True)
    assert result["failed"] == 1 and mail.outbox == []
    u.refresh_from_db()
    assert u.username == "old@example.com"                                    # the whole row was undone, so the old address was not told anything
