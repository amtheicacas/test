"""Review pass 4 (R4-09): A crafted column-matching value crashed the import page (and was saved before it crashed).
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date

import pytest
from django.urls import reverse

from violations.models import Owner, Violation
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


@pytest.mark.parametrize("action", ["apply", "map", "preview", "anything"])
@pytest.mark.parametrize("columns", [{"map_2": "is_superuser"}, {"map_2": "is_superuser", "map_3": "is_superuser"}, {"map_2": "__class__", "map_3": ""}])
def test_R4_09_an_unknown_column_match_is_refused_not_a_server_error_and_not_saved(board, action, columns):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from violations.models import ImportMapping
    csv_bytes = b"Homeowner(s),Address,Zip,Email\nAnn Able,1 Test Way,34240,ann@example.com\n"
    r = board.post(reverse("import_owners"), {"action": "upload", "file": SimpleUploadedFile("o.csv", csv_bytes, content_type="text/csv")})
    assert r.status_code == 200
    r = board.post(reverse("import_owners"), {"action": action, "map_0": "name", "map_1": "property_address", "map_3": "email", **columns})
    assert r.status_code == 200, r.status_code
    assert "does not offer" in r.content.decode()                         # the matching page is shown again, with the reason
    assert ImportMapping.objects.count() == 0 and Owner.objects.count() == 0       # nothing was remembered and nothing imported
