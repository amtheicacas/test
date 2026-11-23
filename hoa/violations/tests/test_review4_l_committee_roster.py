"""Review pass 4 (R4-13): the committee roster page. A double click on "Add" put the same person on the roster twice, and a year typed into a hand-made form
(a very big or a negative number) was saved as it was: on the live PostgreSQL database that raised a server error.
Every test here FAILED on the code before its fix. Nothing is sent anywhere."""
import pytest
from django.urls import reverse

from violations.models import CommitteeMember
from violations.tests.review4_support import board  # noqa: F401  (a fixture)

pytestmark = pytest.mark.django_db


def test_R4_13_pressing_Add_twice_puts_the_same_person_on_the_roster_once(board):
    data = {"action": "add", "name": "New Member", "role": "member", "year": "2026"}
    board.post(reverse("committee_members"), data)
    r = board.post(reverse("committee_members"), data, follow=True)
    assert CommitteeMember.objects.filter(year=2026, name="New Member").count() == 1
    assert "already" in r.content.decode().lower()


def test_R4_13_the_same_name_in_another_year_or_with_different_capitals_is_the_same_person_only_within_a_year(board):
    board.post(reverse("committee_members"), {"action": "add", "name": "New Member", "role": "member", "year": "2026"})
    board.post(reverse("committee_members"), {"action": "add", "name": "New Member", "role": "member", "year": "2027"})
    board.post(reverse("committee_members"), {"action": "add", "name": "new member", "role": "member", "year": "2026"})
    assert sorted(CommitteeMember.objects.values_list("year", flat=True)) == [2026, 2027]


@pytest.mark.parametrize("year", ["99999", "-5", "0", "abc", "1999999999999", ""])
def test_R4_13_a_nonsense_year_is_replaced_by_the_current_year_and_never_a_server_error(board, year):
    r = board.post(reverse("committee_members"), {"action": "add", "name": "Year Tester", "role": "member", "year": year})
    assert r.status_code == 302
    saved = CommitteeMember.objects.get(name="Year Tester")
    assert 2000 <= saved.year <= 2100
