"""One login for owners who are also on the committee or Board; abstaining on your own violation."""
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from violations import accounts, minutes, services
from violations.models import CaseAbstention, CommitteeMeeting, CommitteeMember, Owner, Profile, Violation, ViolationType


@pytest.fixture
def people(db, django_user_model):
    vt = ViolationType.objects.create(name="Dual")
    mine = Owner.objects.create(name="Cam Member", property_address="1 Dual Way", email="cam@example.com")
    other = Owner.objects.create(name="Olly Other", property_address="2 Dual Way", email="olly@example.com")
    cam = django_user_model.objects.create_user("cam@example.com", "cam@example.com", "pw", first_name="Cam")
    Profile.objects.create(user=cam, role="committee", owner=mine, relationship="owner")
    bob = django_user_model.objects.create_user("bob@example.com", "bob@example.com", "pw", first_name="Bob")
    bo = Owner.objects.create(name="Bob Board", property_address="3 Dual Way", email="bob@example.com")
    Profile.objects.create(user=bob, role="board", owner=bo, relationship="owner")
    cam_case = Violation.objects.create(owner=mine, vtype=vt, description="d", cure_required="c", status="notice_sent")
    olly_case = Violation.objects.create(owner=other, vtype=vt, description="d", cure_required="c", status="notice_sent")
    return dict(cam=cam, bob=bob, mine=mine, other=other, cam_case=cam_case, olly_case=olly_case, vt=vt)


def test_one_login_lands_on_the_owners_own_home_with_a_link_to_the_extra_role(client, people):
    client.force_login(people["cam"])
    assert client.get("/").url == "/me/"
    home = client.get("/me/").content.decode()
    assert "Committee &rarr;" in home and "Welcome, Cam" in home
    assert client.get("/committee/").status_code == 200                                 # the committee role still works
    assert "My violations" in client.get("/committee/").content.decode()                # and links back to their own
    client.force_login(people["bob"])
    assert client.get("/").url == "/me/" and "Board / Manager workspace" in client.get("/me/").content.decode()
    assert client.get("/workspace/").status_code == 200 and client.get("/violations/").status_code == 200


def test_the_owner_pages_only_ever_show_your_own_cases_even_with_a_staff_role(client, people):
    for who in ("cam", "bob"):
        client.force_login(people[who])
        assert client.get(f"/me/{people['olly_case'].pk}/").status_code == 404
    client.force_login(people["cam"])
    assert client.get(f"/me/{people['cam_case'].pk}/").status_code == 200
    from violations.models import Notice
    n = Notice.objects.create(violation=people["cam_case"], kind="violation")
    assert client.get(f"/notices/{n.pk}/pdf/").status_code == 200                       # their own letter, though the committee can't see this stage
    n2 = Notice.objects.create(violation=people["olly_case"], kind="violation")
    assert client.get(f"/notices/{n2.pk}/pdf/").status_code == 403


def test_the_two_step_code_is_only_needed_for_the_extra_role(client, people, settings):
    settings.MFA_REQUIRED_ROLES = ("committee", "board", "manager")
    client.force_login(people["cam"])
    assert client.get("/me/").status_code == 200                                         # their own violations: no code needed
    r = client.get("/committee/")
    assert r.status_code == 302 and "/security/" in r.url                                # the committee area asks for it


def test_a_committee_member_can_sign_in_with_the_emailed_link_like_any_owner(client, people):
    mail.outbox.clear()
    client.post("/accounts/get-access/", {"email": "cam@example.com"})
    assert len(mail.outbox) == 1 and "/accounts/link/" in mail.outbox[0].body


def test_picking_owners_for_the_roster_gives_them_the_committee_role_on_their_own_login(client, people, django_user_model):
    y = timezone.localdate().year
    mgr = django_user_model.objects.create_user("mgr60", password="x"); Profile.objects.update_or_create(user=mgr, defaults={"role": "manager"})
    client.force_login(mgr)
    page = client.get(f"/committee/members/?year={y}").content.decode()
    assert "Olly Other (2 Dual Way)" in page and "Pick the member from the owner list" in page
    client.post(f"/committee/members/?year={y}", {"action": "add", "year": y, "owner_pick": f"{people['other'].pk}:1", "role": "chair"})
    m = CommitteeMember.objects.get(name="Olly Other")
    assert m.owner_id == people["other"].pk and m.user is not None and m.role == "chair"
    prof = Profile.objects.get(user=m.user)
    assert prof.owner_id == people["other"].pk and prof.role == "committee"                # same login, committee role added
    assert m.user.username == "olly@example.com"
    # next year they are not on the roster: back to a plain owner login (still active)
    CommitteeMember.objects.filter(pk=m.pk).update(year=y - 1)
    accounts.sync_committee_logins()
    prof.refresh_from_db(); m.user.refresh_from_db()
    assert prof.role == "homeowner" and m.user.is_active
    assert "Olly Other (2 Dual Way)" in client.get(f"/committee/members/?year={y}").content.decode()   # available to pick again


def test_a_member_abstains_on_their_own_violation(client, people, django_user_model):
    y = timezone.localdate().year
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=2), zoom_link="https://z.us/1")
    cam_m = CommitteeMember.objects.create(name="Cam Member", year=y, owner=people["mine"], user=people["cam"])
    others = [CommitteeMember.objects.create(name=n, year=y) for n in ("Ann", "Bea", "Cy")]
    v = people["cam_case"]
    v.status, v.committee_meeting, v.hearing_date, v.proposed_fine = "hearing_scheduled", m, m.starts_at, 100
    v.save()
    assert services.conflicted_members(v) == [cam_m]
    chair = django_user_model.objects.create_user("chair1", password="x"); Profile.objects.update_or_create(user=chair, defaults={"role": "committee"})
    # the member cannot decide their own case
    client.force_login(people["cam"])
    page = client.get(f"/committee/meetings/{m.pk}/").content.decode()
    assert "your own violation" in page
    client.post(f"/committee/cases/{v.pk}/record/", {"outcome": "waive", "findings": "I waive my own."})
    v.refresh_from_db(); assert v.committee_outcome == ""
    # another member records it: the conflicted member is automatically shown as abstaining and appears in the minutes
    client.force_login(chair)
    assert "Must abstain" in client.get(f"/committee/meetings/{m.pk}/").content.decode()
    client.post(f"/committee/cases/{v.pk}/record/", {"outcome": "confirm", "votes_for": 3, "votes_against": 0, "findings": "Seen.", "abstain": [others[0].pk]})
    v.refresh_from_db()
    assert v.committee_outcome == "confirm"
    assert {(a.member.name, a.reason) for a in v.abstentions.all()} == {("Cam Member", "Own violation"), ("Ann", "Abstained")}
    text = " ".join(p for _, ps in minutes.build(m) for p in ps)
    assert "Abstaining: Cam Member (own violation), Ann (abstained)" in text
