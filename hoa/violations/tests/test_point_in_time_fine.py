"""A point-in-time violation starts with a $100 fine filled in on the Board's forms; it is only a starting value."""
import re
from datetime import date

import pytest
from django.contrib.auth import get_user_model

from violations.models import Owner, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
S = Violation.Status
U = get_user_model()


def case(pit, default_fine=250, name="Dog off leash"):
    o = Owner.objects.create(name=f"Owner {Owner.objects.count()}", property_address=f"{Owner.objects.count() + 1} Pit Way", email="o@example.com")
    t = ViolationType.objects.get_or_create(name=name, defaults={"default_cure_days": 15, "default_fine": default_fine})[0]
    return Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.PENDING_BOARD, non_curable=pit,
                                    cure_deadline=date(2026, 1, 1))


def board(client):
    u = U.objects.create_user("b1", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="board")
    client.force_login(u)


def fine_in_queue(html, pk):
    m = re.search(rf'name="fine_{pk}" value="([^"]*)"', html)
    return m.group(1) if m else None


def test_point_in_time_case_starts_at_100_in_the_board_queue_whatever_the_types_usual_fine(client):
    board(client)
    v = case(pit=True, default_fine=250)
    assert fine_in_queue(client.get("/board/").content.decode(), v.pk) == "100.00"


def test_an_ordinary_case_still_starts_from_its_types_usual_fine(client):
    board(client)
    v = case(pit=False, default_fine=250, name="Shed")
    assert fine_in_queue(client.get("/board/").content.decode(), v.pk) == "250.00"


def test_the_single_case_vote_form_is_prefilled_only_for_point_in_time(client):
    board(client)
    pit, plain = case(pit=True), case(pit=False, name="Shed")
    h = client.get(pit.get_absolute_url()).content.decode()
    assert re.search(r'name="proposed_fine"[^>]*value="100"', h) or re.search(r'value="100[^"]*"[^>]*name="proposed_fine"', h)
    h2 = client.get(plain.get_absolute_url()).content.decode()
    assert not re.search(r'name="proposed_fine"[^>]*value="', h2)


def test_the_prefilled_amount_is_only_a_starting_value_the_board_can_change_and_nothing_is_recorded_by_viewing(client):
    board(client)
    v = case(pit=True)
    client.get("/board/")
    client.get(v.get_absolute_url())
    v.refresh_from_db()
    assert v.status == S.PENDING_BOARD and v.proposed_fine is None            # nothing recorded yet
    client.post("/board/bulk/", {"pick": [v.pk], f"decision_{v.pk}": "proceed", f"fine_{v.pk}": "75", f"date_{v.pk}": "2026-02-01", "by_majority": "on"})
    v.refresh_from_db()
    assert v.proposed_fine == 75


def test_the_amount_comes_from_a_setting(client, settings):
    settings.POINT_IN_TIME_FINE = 150
    board(client)
    v = case(pit=True)
    assert fine_in_queue(client.get("/board/").content.decode(), v.pk) == "150.00"
