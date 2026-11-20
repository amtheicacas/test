"""Committee roster (loaded once a year, attendance ticked per meeting) and creating the hearing's Zoom meeting from inside the app."""
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from violations import minutes, services, zoom
from violations.models import CommitteeMeeting, CommitteeMember, MeetingAttendance, Profile


@pytest.fixture
def staff(db, django_user_model, client):
    u = django_user_model.objects.create_user("mgr30", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    return u


def test_roster_is_loaded_once_and_copied_to_next_year(client, staff):
    y = timezone.localdate().year
    client.post(f"/committee/members/?year={y}", {"action": "add", "year": y, "name": "Ann Chair", "role": "chair"})
    client.post(f"/committee/members/?year={y}", {"action": "add", "year": y, "name": "Bob Member", "role": "member"})
    assert CommitteeMember.objects.filter(year=y).count() == 2
    client.post(f"/committee/members/?year={y + 1}", {"action": "copy", "year": y + 1})
    assert set(CommitteeMember.objects.filter(year=y + 1).values_list("name", flat=True)) == {"Ann Chair", "Bob Member"}
    client.post(f"/committee/members/?year={y + 1}", {"action": "copy", "year": y + 1})          # copying twice doesn't duplicate
    assert CommitteeMember.objects.filter(year=y + 1).count() == 2
    html = client.get(f"/committee/members/?year={y}").content.decode()
    assert "Ann Chair" in html and "Chair" in html


def test_only_staff_edit_the_roster_committee_members_read_it(client, django_user_model):
    u = django_user_model.objects.create_user("cm30", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "committee"})
    client.force_login(u)
    y = timezone.localdate().year
    assert client.get("/committee/members/").status_code == 200
    assert client.post("/committee/members/", {"action": "add", "year": y, "name": "Sneaky"}).status_code == 403
    assert not CommitteeMember.objects.exists()


def test_meeting_page_lists_the_roster_and_minutes_use_the_ticks(client, staff):
    y = timezone.localdate().year
    for n in ("Ann", "Bob", "Cy", "Dee"):
        CommitteeMember.objects.create(name=n, year=y, role="chair" if n == "Ann" else "member")
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=2), zoom_link="https://z.us/1")
    page = client.get(f"/committee/meetings/{m.pk}/").content.decode()
    assert "Ann" in page and 'name="attend_' in page and "0 present" in page
    ids = {mem.name: mem.pk for mem in CommitteeMember.objects.all()}
    client.post(f"/committee/meetings/{m.pk}/save/", {f"attend_{ids['Ann']}": "present", f"attend_{ids['Bob']}": "present", f"attend_{ids['Cy']}": "present",
                                                       f"attend_{ids['Dee']}": "excused", "guests": "Owner at 5 Main", "attendees": "Guest member Eve"})
    assert MeetingAttendance.objects.filter(meeting=m, status="present").count() == 3
    m.refresh_from_db()
    text = " ".join(p for _, ps in minutes.build(m) for p in ps)
    assert "Ann (Chair), Bob, Cy, Guest member Eve" in text and "Dee (excused)" in text and "4 members were present" in text
    client.post(f"/committee/meetings/{m.pk}/save/", {f"attend_{ids['Ann']}": "absent"})          # ticks can be changed; blank clears the rest
    assert MeetingAttendance.objects.get(meeting=m, member_id=ids["Ann"]).status == "absent" and MeetingAttendance.objects.filter(meeting=m).count() == 1


def test_a_meeting_without_its_own_year_falls_back_to_the_latest_roster(db):
    y = timezone.localdate().year
    CommitteeMember.objects.create(name="Old Timer", year=y - 1)
    m = CommitteeMeeting.objects.create(starts_at=timezone.now(), zoom_link="https://z.us/1")
    assert [x.name for x in m.roster()] == ["Old Timer"]


# ---------------- Zoom ----------------
@pytest.fixture
def zoom_on(settings, db):
    settings.ZOOM_ACCOUNT_ID, settings.ZOOM_CLIENT_ID, settings.ZOOM_CLIENT_SECRET = "acct", "cid", "secret"
    zoom._token.update(value="", expires=0.0)


ZOOM_REPLY = {"id": 85551234567, "join_url": "https://us02web.zoom.us/j/85551234567?pwd=abc", "password": "p4ss",
              "settings": {"global_dial_in_numbers": [{"number": "+1 929 205 6099", "city": "New York"}, {"number": "+1 301 715 8592", "city": "Washington DC"}]}}


def fake_zoom(calls):
    def _req(method, url, body=None, headers=None):
        calls.append((method, url, body))
        return {"access_token": "tok", "expires_in": 3600} if "oauth/token" in url else ZOOM_REPLY
    return _req


def test_creating_a_meeting_creates_the_zoom_meeting_and_fills_in_everything(zoom_on):
    calls = []
    start = timezone.now() + timedelta(days=21)
    with mock.patch.object(zoom, "_request", side_effect=fake_zoom(calls)):
        m = services.create_meeting(start, create_zoom=True)
    assert m.zoom_link == ZOOM_REPLY["join_url"] and m.zoom_meeting_id == "85551234567" and m.zoom_passcode == "p4ss" and m.zoom_api_id == "85551234567"
    assert "+1 929 205 6099 New York" in m.zoom_dial_in
    token_call, create_call = calls
    assert "grant_type=account_credentials" in token_call[1] and "acct" in token_call[1]
    assert create_call[0] == "POST" and "/v2/users/me/meetings" in create_call[1]
    assert create_call[2]["type"] == 2 and create_call[2]["timezone"] == "America/New_York" and create_call[2]["settings"]["waiting_room"] is True


def test_a_pasted_link_is_used_and_zoom_is_not_called(zoom_on):
    with mock.patch.object(zoom, "_request", side_effect=AssertionError("Zoom must not be called")):
        m = services.create_meeting(timezone.now() + timedelta(days=20), zoom_link="https://zoom.us/j/1", create_zoom=True)
    assert m.zoom_link == "https://zoom.us/j/1" and not m.zoom_api_id


def test_zoom_failure_is_explained_and_a_location_still_lets_the_meeting_be_created(zoom_on):
    start = timezone.now() + timedelta(days=20)
    with mock.patch.object(zoom, "_request", side_effect=OSError("boom")):
        with pytest.raises(services.WorkflowError, match="Zoom did not create"):
            services.create_meeting(start, create_zoom=True)
        m = services.create_meeting(start, location="Clubhouse", create_zoom=True)
    assert m.location == "Clubhouse" and not m.zoom_link


def test_zoom_not_connected_says_so(db, settings):
    settings.ZOOM_ACCOUNT_ID = ""
    with pytest.raises(services.WorkflowError, match="not connected"):
        services.create_meeting(timezone.now() + timedelta(days=20), create_zoom=True)


def test_meetings_page_form_creates_the_zoom_meeting(client, staff, zoom_on):
    start = (timezone.localtime(timezone.now()) + timedelta(days=21)).strftime("%Y-%m-%dT%H:%M")
    with mock.patch.object(zoom, "_request", side_effect=fake_zoom([])):
        assert "Zoom is connected" in client.get("/meetings/").content.decode()
        client.post("/meetings/", {"starts_at": start, "create_zoom": "on"})
    assert CommitteeMeeting.objects.get().zoom_link == ZOOM_REPLY["join_url"]


def test_owner_sees_a_hearing_banner_with_days_remaining_and_only_their_own_items(client, django_user_model):
    from violations.models import Owner, ViolationType, Violation
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=9, hours=2), zoom_link="https://z.us/9", zoom_meeting_id="123", zoom_passcode="pw")
    vt = ViolationType.objects.create(name="Mine")
    mine, theirs = (Owner.objects.create(name=n, property_address=a) for n, a in (("Pat", "1 Mine Way"), ("Sam", "2 Theirs Way")))
    v1 = Violation.objects.create(owner=mine, vtype=vt, description="d", cure_required="c", status="hearing_scheduled", committee_meeting=m, hearing_date=m.starts_at, proposed_fine=50)
    Violation.objects.create(owner=mine, vtype=ViolationType.objects.create(name="Mine2"), description="d", cure_required="c", status="hearing_scheduled", committee_meeting=m, hearing_date=m.starts_at)
    v3 = Violation.objects.create(owner=theirs, vtype=ViolationType.objects.create(name="SECRET-THEIRS"), description="d", cure_required="c", status="hearing_scheduled", committee_meeting=m, hearing_date=m.starts_at)
    u = django_user_model.objects.create_user("pat1", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": mine})
    client.force_login(u)
    home = client.get("/me/").content.decode()
    assert "You have a homeowner hearing in 9 days" in home and "9 days remaining" in home and "2 violations on your agenda" in home
    page = client.get(f"/me/hearings/{m.pk}/").content.decode()
    assert "Mine" in page and "Mine2" in page and "Join by Zoom" in page and "9 days remaining" in page
    assert "SECRET-THEIRS" not in page and "Sam" not in page and "2 Theirs Way" not in page      # never anyone else's item
    assert "SECRET-THEIRS" not in home
    s = django_user_model.objects.create_user("sam1", password="x"); Profile.objects.update_or_create(user=s, defaults={"role": "homeowner", "owner": theirs})
    client.force_login(s)
    other = client.get(f"/me/hearings/{m.pk}/").content.decode()
    assert "SECRET-THEIRS" in other and "Mine2" not in other and "1 Mine Way" not in other
    nobody = Owner.objects.create(name="Nope", property_address="3 None Way")
    z = django_user_model.objects.create_user("nope1", password="x"); Profile.objects.update_or_create(user=z, defaults={"role": "homeowner", "owner": nobody})
    client.force_login(z)
    assert client.get(f"/me/hearings/{m.pk}/").status_code == 404 and "homeowner hearing" not in client.get("/me/").content.decode()
