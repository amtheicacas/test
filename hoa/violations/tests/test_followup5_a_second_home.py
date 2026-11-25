"""Follow-up after review pass 4 (owner's answer `second-home`): a Board member, manager or committee member whose own email address is listed on a
home's owner record is deciding on THEIR OWN household's case, even when that home is not the one their login is attached to (a second home).
Every test here FAILED on the code before the change. Nothing is sent anywhere."""
from datetime import date, timedelta

import pytest
from django.utils import timezone

from violations import services, sitevisits
from violations.models import CommitteeMeeting, CommitteeMember, Owner, Profile, Violation, ViolationType

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db


@pytest.fixture
def two_homes(django_user_model):
    vt = ViolationType.objects.create(name="Second", default_cure_days=15)
    first = Owner.objects.create(name="Bob Board", property_address="3 Dual Way", email="bob@example.com")
    second = Owner.objects.create(name="Bob Board", property_address="9 Other Way", email="Bob@Example.com ", additional_emails="bobby@example.com")
    co_owned = Owner.objects.create(name="Cy Co", property_address="7 Co Way", email="cy@example.com", co_owner_email="bob@example.com")
    stranger = Owner.objects.create(name="Sam Stranger", property_address="1 Far Way", email="sam@example.com")
    bob = django_user_model.objects.create_user("bob", "bob@example.com", "pw")
    Profile.objects.create(user=bob, role="board", owner=first)

    def case(owner, status=S.NOTICE_SENT, **kw):
        return Violation.objects.create(owner=owner, vtype=vt, description="d", cure_required="c", status=status,
                                        cure_deadline=TODAY + timedelta(days=10), **kw)
    return bob, first, second, co_owned, stranger, case


def test_F5_second_home_is_your_own_household(two_homes):
    bob, first, second, co_owned, stranger, case = two_homes
    assert services.own_household(bob, case(first))                                     # the home the login is attached to (as before)
    assert services.own_household(bob, case(second))                                    # the second home lists the same address (any capitals, stray spaces)
    assert services.own_household(bob, case(co_owned))                                  # listed as the co-owner's email
    assert not services.own_household(bob, case(stranger))


def test_F5_any_of_the_extra_addresses_on_the_owner_record_count(two_homes, django_user_model):
    _, _, second, _, _, case = two_homes
    bobby = django_user_model.objects.create_user("bobby", "bobby@example.com", "pw")
    Profile.objects.create(user=bobby, role="manager")
    assert services.own_household(bobby, case(second))                                  # listed under "more owner emails"


def test_F5_a_login_with_no_email_address_never_matches(django_user_model):
    vt = ViolationType.objects.create(name="NoMail", default_cure_days=15)
    home = Owner.objects.create(name="No Mail", property_address="5 Blank Way", email="")
    v = Violation.objects.create(owner=home, vtype=vt, description="d", cure_required="c", status=S.NOTICE_SENT, cure_deadline=TODAY + timedelta(days=5))
    u = django_user_model.objects.create_user("nomail", "", "pw")
    Profile.objects.create(user=u, role="manager")
    assert not services.own_household(u, v)


def test_F5_the_decisions_are_refused_on_a_second_home(two_homes):
    bob, _, second, _, _, case = two_homes
    v = case(second)
    with pytest.raises(services.WorkflowError, match="your own household"):
        services.dismiss(v, bob, "no")
    with pytest.raises(services.WorkflowError, match="your own household"):
        services.grant_extension(v, days=10, user=bob)


def test_F5_a_committee_member_must_abstain_on_a_second_home(two_homes, django_user_model):
    bob, first, second, _, _, case = two_homes
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/1")
    CommitteeMember.objects.create(name="Bob Board", year=timezone.localtime(meeting.starts_at).year, user=bob)         # on the roster by login only
    v = case(second, S.HEARING_SCHEDULED, committee_meeting=meeting, hearing_date=meeting.starts_at)
    assert bob.pk in [m.user_id for m in services.conflicted_members(v)]
    other = case(Owner.objects.create(name="Zed", property_address="2 Zed Way", email="zed@example.com"), S.HEARING_SCHEDULED,
                 committee_meeting=meeting, hearing_date=meeting.starts_at)
    assert services.conflicted_members(other) == []


def test_F5_site_visit_notes_are_blocked_on_a_second_home(two_homes):
    bob, _, second, _, stranger, case = two_homes
    assert sitevisits.blocked_reason(bob, case(second)) == "own"
    assert sitevisits.blocked_reason(bob, case(stranger)) is None
