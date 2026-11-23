"""Review pass 4 (R4-11): PostgreSQL enforces column lengths (SQLite does not): three places stored text longer than their column.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date

import pytest
from django.core import mail
from django.urls import reverse

from violations import services
from violations.models import CommitteeMember, EmailLog, Notice, Owner, Violation
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


@pytest.mark.parametrize("count", [5, 12])      # 5 addresses are longer than the notice's "emailed to" column (254) but fit the email log's (500); 12 overflow both
def test_R4_11_a_household_with_many_email_addresses_can_still_be_sent_a_notice(settings, count):
    settings.MAIL_PROVIDER = "manual"
    many = ";".join(f"a-rather-long-family-member-{i:02d}@example-mail-provider.com" for i in range(count))
    v = make_case(S.DRAFT, owner_kw={"additional_emails": many})
    n = services.send_notice(v, Notice.Kind.VIOLATION, None)
    n = Notice.objects.get(pk=n.pk)
    assert len(n.emailed_to) <= Notice._meta.get_field("emailed_to").max_length
    assert n.emailed_to.startswith("p1@example.com")
    assert len(mail.outbox) == 1 and len(mail.outbox[0].to) == count + 1                                  # every address was still emailed
    assert all(len(e.to) <= EmailLog._meta.get_field("to").max_length for e in EmailLog.objects.all())


def test_R4_11_a_meeting_place_longer_than_the_hearing_location_column_is_cut_to_fit(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=TODAY)
    m = future_meeting(30, zoom_link="https://zoom.example/j/" + "1" * 170, location="The big hall at the end of the long road " * 5)
    assert len(m.place) > Violation._meta.get_field("hearing_location").max_length
    services.assign_to_meeting(v, m, None)
    v.refresh_from_db()
    assert 0 < len(v.hearing_location) <= Violation._meta.get_field("hearing_location").max_length


def test_R4_11_an_owner_with_a_long_name_can_be_added_to_the_committee_roster(board):
    name = "The John and Mary Q. Public Revocable Living Trust dated March 3, 2015, John Public and Mary Public, Trustees"      # 116 characters
    name = (name + ", " + name)[:190]                                                       # an owner's name may be up to 200 characters; the roster's column holds 120
    o = Owner.objects.create(name=name, property_address="3 Trust Way", email="t@example.com")
    assert len(o.name) > CommitteeMember._meta.get_field("name").max_length
    board.post(reverse("committee_members"), {"action": "add", "owner_pick": f"{o.pk}:1", "role": "member", "year": "2026"})
    cm = CommitteeMember.objects.get(owner=o)
    assert 0 < len(cm.name) <= CommitteeMember._meta.get_field("name").max_length


def test_R4_11_a_reminder_to_a_household_with_dozens_of_addresses_still_fits_the_email_log():
    many = ";".join(f"family-member-number-{i:03d}@example-mail-provider.com" for i in range(40))
    v = make_case(S.NOTICE_SENT, owner_kw={"additional_emails": many})
    sent = services._email_owner(v, "Reminder", "Please fix it.")
    assert len(sent) == 41                                                                     # everybody was emailed
    assert all(len(e.to) <= EmailLog._meta.get_field("to").max_length for e in EmailLog.objects.all())


def test_R4_11_fit_addresses_keeps_whole_addresses_and_says_how_many_more():
    one = ["a@b.co"]
    assert services.fit_addresses(one, 254) == "a@b.co" and services.fit_addresses([], 254) == ""
    exact = ["aaaa@x.co", "bbbb@x.co"]
    assert services.fit_addresses(exact, len(", ".join(exact))) == ", ".join(exact)                    # exactly fits: untouched
    out = services.fit_addresses([f"person{i}@example.org" for i in range(30)], 80)
    assert len(out) <= 80 and out.endswith("more)") and out.startswith("person0@example.org, person1@example.org")
    shown = out.split(" (+")[0].split(", ")
    assert all(a.endswith("@example.org") for a in shown)                                         # no address is cut in half
    assert len(services.fit_addresses(["x" * 500 + "@example.org"], 100)) <= 100                   # even one huge address fits


def test_R4_11_cut_to_only_shortens_what_does_not_fit():
    assert services.cut_to("abc", 3) == "abc" and services.cut_to("abcd", 3) == "ab…" and services.cut_to(None, 5) == ""
