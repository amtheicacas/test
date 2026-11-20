"""Stabilisation pass: tests for bugs found by reading the code. One test (or more) per fix."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from violations.models import CommitteeMeeting, CommitteeMember, Owner, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
U = get_user_model()
S = Violation.Status


def mk(name, role, owner=None):
    u = U.objects.create_user(name, f"{name}@example.com", "x", first_name=name.title(), last_name="T")
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


def past_hearing_case(owner):
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=2), zoom_link="https://z.example/1")
    t = ViolationType.objects.get_or_create(name="Trash cans", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=owner, vtype=t, description="d", cure_required="c", status=S.HEARING_SCHEDULED, proposed_fine=100,
                                 committee_meeting=m, hearing_date=m.starts_at)
    return m, v


def test_a_committee_member_cannot_vote_on_their_own_violation_through_the_old_vote_page(client):
    """F-01: the old /committee/cases/<id>/vote/ page had no abstention check, so an owner who also sits on the committee could decide
    (reject) their own fine. The meeting page already blocks this; the old page must too."""
    me = Owner.objects.create(name="Dual Member", property_address="1 Own Way", email="dual@example.com")
    m, v = past_hearing_case(me)
    u = mk("dual", "committee", me)
    CommitteeMember.objects.create(name="Dual Member", year=timezone.localdate().year, user=u, owner=me)
    client.force_login(u)
    r = client.post(f"/committee/cases/{v.pk}/vote/", {"votes_for": 0, "votes_against": 3, "findings": "I reject my own fine."}, follow=True)
    v.refresh_from_db()
    assert v.status == S.HEARING_SCHEDULED and v.committee_votes_for is None
    assert "your own violation" in r.content.decode()


def test_a_household_member_on_the_roster_cannot_vote_either(client):
    owner = Owner.objects.create(name="Pat Owner", property_address="2 Own Way", email="pat@example.com")
    m, v = past_hearing_case(owner)
    spouse = mk("spouse", "committee")                                         # a different login, but the roster says it is the same household
    CommitteeMember.objects.create(name="Spouse Owner", year=timezone.localdate().year, user=spouse, owner=owner)
    client.force_login(spouse)
    client.post(f"/committee/cases/{v.pk}/vote/", {"votes_for": 0, "votes_against": 3, "findings": "x"})
    v.refresh_from_db()
    assert v.status == S.HEARING_SCHEDULED


def test_an_unrelated_committee_member_can_still_use_the_old_vote_page_as_before(client):
    """The old page keeps working for everyone else: this fix only stops the conflict of interest."""
    other = Owner.objects.create(name="Someone Else", property_address="3 Other Way", email="se@example.com")
    m, v = past_hearing_case(other)
    u = mk("member", "committee")
    CommitteeMember.objects.create(name="Member", year=timezone.localdate().year, user=u)
    client.force_login(u)
    client.post(f"/committee/cases/{v.pk}/vote/", {"votes_for": 3, "votes_against": 0, "findings": "Confirmed."})
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.committee_votes_for == 3
