"""Review pass 4 (R4-22): found by the real-browser phone-width check. The staff case page shows "Print/mail provider: lob <ids>" for a notice handed to the print service, and the ids of its
letters were joined with commas and NO spaces ("ltr_a,ltr_b,ltr_c,ltr_d": about 80 unbreakable characters for the four letters of a hearing notice), so on a phone the page was wider than the
screen and had to be scrolled sideways. They are now listed with a space after each comma so the line can wrap. Nothing is sent anywhere."""
from datetime import date

import pytest

from violations.models import Notice, Violation
from violations.tests.review4_support import make_case

pytestmark = pytest.mark.django_db
S = Violation.Status


def test_R4_22_the_print_service_letter_ids_can_wrap_on_a_narrow_screen():
    v = make_case(S.HEARING_SCHEDULED)
    ids = ",".join(f"ltr_17954757{i}7777" for i in range(4))
    n = Notice.objects.create(violation=v, kind="hearing", provider="lob", provider_ids=ids, mailed_on=date.today())
    line = next(x for x in n.delivery_lines(staff=True) if x.startswith("Print/mail provider"))
    assert all(len(word) <= 22 for word in line.split())                          # no word longer than one id: the line can wrap anywhere between ids
    assert line.count("ltr_") == 4 and n.provider_ids == ids                      # (what is stored is unchanged)
    assert not any(x.startswith("Print/mail provider") for x in n.delivery_lines(staff=False))     # and owners never see it
