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
    settings.ZOOM_HOST_EMAIL = "host@example.org"
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
    assert create_call[0] == "POST" and "/v2/users/host%40example.org/meetings" in create_call[1]
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
        page = client.get("/meetings/").content.decode()
        assert "Create the Zoom meeting for me" in page and 'id="id_create_zoom" checked' in page
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


def test_zoom_needs_a_host_email(zoom_on, settings):
    settings.ZOOM_HOST_EMAIL = ""
    with pytest.raises(services.WorkflowError, match="ZOOM_HOST_EMAIL"):
        services.create_meeting(timezone.now() + timedelta(days=20), create_zoom=True)


def test_zoom_meeting_settings_recording_cameras_and_alternative_hosts(zoom_on, settings):
    settings.ZOOM_ALTERNATIVE_HOSTS = "alt1@example.org, alt2@example.org"
    calls = []
    with mock.patch.object(zoom, "_request", side_effect=fake_zoom(calls)):
        services.create_meeting(timezone.now() + timedelta(days=21), create_zoom=True)
    s = calls[1][2]["settings"]
    assert s["auto_recording"] == "cloud" and s["host_video"] is True and s["participant_video"] is True
    assert s["alternative_hosts"] == "alt1@example.org,alt2@example.org" and s["waiting_room"] is True


def test_series_creates_one_meeting_and_one_zoom_meeting_per_date(zoom_on):
    from datetime import datetime
    calls = []
    third_monday = timezone.make_aware(datetime(2027, 1, 18, 18, 0))                  # the 3rd Monday of January 2027
    with mock.patch.object(zoom, "_request", side_effect=fake_zoom(calls)):
        made, err = services.create_meeting_series(third_monday, 4, create_zoom=True)
    assert err == "" and len(made) == 4 and {m.series for m in made} == {"3rd Monday of each month"}
    local = [timezone.localtime(m.starts_at) for m in made]
    assert [(d.month, d.day, d.hour) for d in local] == [(1, 18, 18), (2, 15, 18), (3, 15, 18), (4, 19, 18)]
    assert all(d.weekday() == 0 for d in local) and len([c for c in calls if "/meetings" in c[1]]) == 4    # one Zoom meeting each


def test_a_fifth_weekday_means_the_last_one(db):
    from datetime import datetime
    label, dates = services.series_dates(timezone.make_aware(datetime(2027, 3, 29, 18, 0)), 3)      # 5th Monday of March 2027
    assert label == "last Monday of each month" and [(d.month, d.day) for d in dates] == [(3, 29), (4, 26), (5, 31)]


def test_assign_selected_cases_to_a_picked_future_meeting_and_the_notice_carries_that_meetings_zoom(client, staff):
    from violations.models import Owner, Notice, ViolationType, Violation
    from violations.pdf import render_notice_pdf
    import subprocess, tempfile, os
    soon = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://zoom.us/j/AAA111", zoom_meeting_id="111 111", zoom_passcode="aaa")
    later = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=60), zoom_link="https://zoom.us/j/BBB222", zoom_meeting_id="222 222", zoom_passcode="bbb")
    vt = ViolationType.objects.create(name="Batch")
    cases = []
    for i in range(3):
        o = Owner.objects.create(name=f"O{i}", property_address=f"{i + 1} Batch Way", email=f"o{i}@example.com")
        cases.append(Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="hearing_needed", proposed_fine=100, board_meeting_date=timezone.localdate()))
    page = client.get("/meetings/").content.decode()
    assert "3 Board-approved violations waiting" in page and "Assign selected" in page and "days away" in page
    client.post("/meetings/assign-selected/", {"meeting": later.pk, "pick": [cases[0].pk, cases[1].pk]})
    for v in cases:
        v.refresh_from_db()
    assert [v.status for v in cases] == ["hearing_scheduled", "hearing_scheduled", "hearing_needed"]      # the unticked one stays waiting
    assert cases[0].committee_meeting_id == later.pk
    n = cases[0].notices.get(kind="hearing")
    pdf = render_notice_pdf(n, "owner")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf)
    text = " ".join(subprocess.run(["pdftotext", f.name, "-"], capture_output=True, text=True).stdout.split())
    os.unlink(f.name)
    assert "https://zoom.us/j/BBB222" in text and "222 222" in text and "bbb" in text and "AAA111" not in text
    from django.core import mail
    body = " ".join(m.body for m in mail.outbox if "hearing" in m.subject.lower())
    assert mail.outbox and all(m.attachments for m in mail.outbox if "hearing" in m.subject.lower())     # the same PDF is attached to the email


def test_creating_a_meeting_under_twenty_days_warns(client, staff):
    start = (timezone.localtime(timezone.now()) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/meetings/", {"starts_at": start, "zoom_link": "https://zoom.us/j/1"}, follow=True)
    import html as _h
    text = _h.unescape(r.content.decode())
    assert "under 20 days away" in text and "14 days' notice is required" in text
    page = client.get("/meetings/").content.decode()
    assert "days' notice is required for a hearing" in page                                        # the pop-up text is on the page


def _assigned(client, days=40):
    from violations.models import Owner, ViolationType, Violation
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=days), zoom_link="https://zoom.us/j/OLD", zoom_api_id="999")
    o = Owner.objects.create(name="Move Me", property_address="9 Move Way", email="mv@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="Mv"), description="d", cure_required="c", status="hearing_needed",
                                 proposed_fine=100, board_meeting_date=timezone.localdate())
    services.assign_to_meeting(v, m)
    v.refresh_from_db()
    return m, v


def test_meetings_are_clickable_and_can_be_rescheduled_with_new_notices_and_zoom_updated(client, staff, zoom_on):
    from django.core import mail
    m, v = _assigned(client)
    assert f"/meetings/{m.pk}/" in client.get("/meetings/").content.decode()
    assert "Reschedule" in client.get(f"/meetings/{m.pk}/").content.decode()
    new = (timezone.localtime(timezone.now()) + timedelta(days=45)).replace(microsecond=0)
    calls = []
    mail.outbox.clear()
    with mock.patch.object(zoom, "_request", side_effect=fake_zoom(calls)):
        client.post(f"/meetings/{m.pk}/", {"action": "reschedule", "starts_at": new.strftime("%Y-%m-%dT%H:%M"), "notify": "on"})
    m.refresh_from_db(); v.refresh_from_db()
    assert timezone.localtime(m.starts_at).replace(second=0) == new.replace(second=0) and v.hearing_date == m.starts_at
    assert v.notices.filter(kind="hearing").count() == 2 and mail.outbox                       # a fresh certified + email notice
    patch = [c for c in calls if c[0] == "PATCH"][0]
    assert patch[1].endswith("/v2/meetings/999") and patch[2]["start_time"].startswith(new.strftime("%Y-%m-%dT%H:%M"))


def test_rescheduling_needs_fourteen_days_notice_when_cases_are_assigned(client, staff):
    m, v = _assigned(client)
    client.post(f"/meetings/{m.pk}/", {"action": "reschedule", "starts_at": (timezone.localtime(timezone.now()) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")})
    m.refresh_from_db()
    assert (m.starts_at - timezone.now()).days >= 39


def test_cancelling_returns_cases_to_waiting_emails_owners_and_deletes_the_zoom_meeting(client, staff, zoom_on):
    from django.core import mail
    m, v = _assigned(client)
    mail.outbox.clear()
    calls = []
    with mock.patch.object(zoom, "_request", side_effect=fake_zoom(calls)):
        client.post(f"/meetings/{m.pk}/", {"action": "cancel", "notify": "on"})
    v.refresh_from_db()
    assert not CommitteeMeeting.objects.filter(pk=m.pk).exists()
    assert v.status == "hearing_needed" and v.committee_meeting_id is None and v.hearing_date is None
    assert any("cancelled" in x.subject.lower() for x in mail.outbox)
    assert [c[0] for c in calls if "/meetings/999" in c[1]] == ["DELETE"]


def test_a_meeting_with_decisions_cannot_be_moved_or_cancelled(client, staff):
    m, v = _assigned(client)
    v.committee_outcome = "confirm"; v.save()
    client.post(f"/meetings/{m.pk}/", {"action": "cancel"})
    assert CommitteeMeeting.objects.filter(pk=m.pk).exists()
