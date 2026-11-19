import re
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from violations import services
from violations.models import CommitteeMeeting, Notice, Owner, Profile, Response, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()
TODAY = date.today()


def login(client, role, owner=None):
    u = U.objects.create_user(f"{role}{U.objects.count()}", password="pw-for-tests-123")
    Profile.objects.create(user=u, role=role, owner=owner)
    client.force_login(u)


def vt(name="Trash cans", section="3.22"):
    return ViolationType.objects.get_or_create(name=name, defaults={"section": section})[0]


def mk(owner, vtype, status=S.NOTICE_SENT, days_ago=0, **kw):
    v = Violation.objects.create(owner=owner, vtype=vtype, description="d", cure_required="c", status=status,
                                 cure_deadline=TODAY + timedelta(days=5), **kw)
    n = Notice.objects.create(violation=v, kind="violation")
    Notice.objects.filter(pk=n.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    Violation.objects.filter(pk=v.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    v.refresh_from_db()
    return v


def owner(n=1):
    return Owner.objects.create(name=f"O{n}", property_address=f"{n} A St", lot_no=str(n))


# ---------- owner history ----------
def test_history_counts_same_type_open_others_past_and_fines():
    o, t = owner(), vt()
    other_type = vt("Parking", "3.60")
    old1 = mk(o, t, S.CURED, days_ago=400, cured_on=TODAY - timedelta(days=380))
    old2 = mk(o, t, S.CLOSED, days_ago=200, fine_amount=100, cured_on=TODAY - timedelta(days=150))
    open_other = mk(o, other_type, S.PENDING_BOARD, days_ago=20)
    draft = mk(o, t, S.DRAFT)
    v = mk(o, t, days_ago=1)
    other_owner_case = mk(owner(2), t)
    h = services.owner_history(v)
    assert h["same_type"] == [old2, old1] and h["occurrence"] == 3                     # the 3rd time for this violation type
    assert h["open_others"] == [open_other] and h["past"] == [old2, old1]
    assert h["total"] == 4 and h["fines_total"] == 100 and h["last_12_months"] == 3     # drafts and other owners don't count
    assert draft not in h["others"] and other_owner_case not in h["others"]
    assert h["repeat_of"] is None                                                       # last cure was 150 days before: not within 3 months


def test_repeat_within_three_months_of_a_cure_is_flagged():
    o, t = owner(), vt()
    cured = mk(o, t, S.CURED, days_ago=60, cured_on=TODAY - timedelta(days=30))
    v = mk(o, t, days_ago=0)
    assert services.owner_history(v)["repeat_of"] == cured
    far = mk(owner(2), t, S.CURED, days_ago=300, cured_on=TODAY - timedelta(days=200))
    assert services.owner_history(mk(far.owner, t))["repeat_of"] is None


def test_related_violations_share_a_standards_section():
    o = owner()
    a, b = vt("Cans visible", "3.22"), vt("Cans out early", "3.22")
    sib = mk(o, b)
    h = services.owner_history(mk(o, a))
    assert h["same_section"] == [sib] and h["same_type"] == []


def test_first_violation_has_a_clean_history():
    h = services.owner_history(mk(owner(), vt()))
    assert (h["occurrence"], h["total"], h["open_others"], h["same_type"], h["fines_total"]) == (1, 1, [], [], 0)


def test_the_history_shows_inside_the_violation_for_staff(client):
    o, t = owner(), vt()
    mk(o, t, S.CLOSED, days_ago=200, fine_amount=100, cured_on=TODAY - timedelta(days=150))
    mk(o, vt("Parking", "3.60"), S.PENDING_BOARD, days_ago=10)
    v = mk(o, t)
    for role in ("manager", "board"):
        login(client, role)
        page = client.get(f"/violations/{v.pk}/").content.decode()
        assert "Owner history" in page and "2nd" in page and "time for this violation type" in page
        assert "Same violation before (1)" in page and "Other open violations (1)" in page and "Parking" in page and "$100" in page


def test_repeat_banner_and_standards_note_for_landscaping(client):
    o = owner()
    t = vt("Lawn", "4.02")
    mk(o, t, S.CURED, days_ago=50, cured_on=TODAY - timedelta(days=20))
    v = mk(o, t)
    login(client, "manager")
    page = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Repeat violation" in page and "4.02(i)(3)" in page


def test_the_history_is_not_shown_to_committee_or_homeowners(client):
    o, t = owner(), vt()
    v = mk(o, t, S.HEARING_SCHEDULED)
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=20), zoom_link="https://z.us/1")
    v.committee_meeting, v.hearing_date = m, m.starts_at
    v.save()
    login(client, "committee")
    assert "Owner history" not in client.get(f"/committee/cases/{v.pk}/").content.decode()
    login(client, "homeowner", owner=o)
    assert "Owner history" not in client.get(f"/me/{v.pk}/").content.decode()


def test_board_queue_shows_a_history_line(client):
    o, t = owner(), vt()
    mk(o, t, S.CLOSED, days_ago=200, fine_amount=100, cured_on=TODAY - timedelta(days=150))
    mk(o, t, S.PENDING_BOARD, days_ago=20)
    login(client, "board")
    page = client.get("/board/").content.decode()
    assert "2nd" in page and "time</b>" in page.replace("</b> time", "time</b>") and "$100 prior fines" in page


# ---------- menu counts ----------
def nav(page):
    return {m.group(1): re.sub(r"<[^>]+>|&nbsp;", "", m.group(2)).strip() for m in
            re.finditer(r'<a class="nav" href="[^"]*"[^>]*><span>([^<]+)</span><small[^>]*>(.*?)</small>', page)}


def test_staff_menu_shows_a_count_under_each_heading(client):
    owners = [owner(i) for i in range(1, 6)]
    mk(owners[0], vt()); mk(owners[1], vt(), S.CURED); mk(owners[2], vt(), S.PENDING_BOARD); mk(owners[3], vt(), S.HEARING_NEEDED)
    mk(owners[4], vt(), S.LEDGER_PENDING); mk(owners[0], vt("Parking", "3.60"), S.DRAFT)
    CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=20), zoom_link="https://z.us/1")
    CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=60), zoom_link="https://z.us/0")   # past: not counted
    Owner.objects.create(name="Gone", property_address="9 Old St", active=False)
    v = Violation.objects.filter(status=S.NOTICE_SENT).first()
    services.add_response(v, "More time please", from_resident=True, extension_requested=True, extension_days=5)
    login(client, "manager")
    n = nav(client.get("/").content.decode())
    assert n["Violations"] == "4 open"                       # open, drafts and closed excluded
    assert n["Board queue"] == "3"                           # 1 awaiting vote + 1 waiting for hearing + 1 awaiting posting
    assert n["Committee meetings"] == "1 upcoming" and n["Map"] == "5 homes"
    assert n["Dashboard"] == "4 to do"                       # the 3 board items + 1 extension request
    assert "+ New" in n


def test_menu_counts_at_zero_and_titles(client):
    login(client, "board")
    page = client.get("/").content.decode()
    n = nav(page)
    assert n["Violations"] == "0 open" and n["Board queue"] == "0" and n["Dashboard"] == "0 to do"
    assert 'title="0 awaiting a Board vote, 0 waiting for a hearing, 0 awaiting approval to post"' in page


def test_committee_and_homeowner_menus_have_counts_too(client):
    o = owner()
    mk(o, vt()); mk(o, vt(), S.CURED); mk(o, vt("Parking", "3.60"), S.DRAFT)
    CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=20), zoom_link="https://z.us/1")
    login(client, "committee")
    assert nav(client.get("/committee/").content.decode()) == {"Committee meetings": "1 upcoming"}
    login(client, "homeowner", owner=o)
    assert nav(client.get("/me/").content.decode()) == {"Home": "", "Violations": "1 open"}


def test_logged_out_pages_have_no_menu(client):
    assert 'class="nav"' not in client.get("/accounts/login/").content.decode()


def test_map_has_resident_label_and_form_has_owner_finder(client, django_user_model):
    from violations import accounts  # noqa: F401
    u = django_user_model.objects.create_user("mgr2", password="x")
    from violations.models import Profile
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    html = client.get("/map/").content.decode()
    assert 'id="lbl-name"' in html and "a box per house" not in html
    form = client.get("/violations/new/").content.decode()
    assert 'id="oq"' in form


def test_label_text_includes_resident_name():
    import json, subprocess, pathlib
    js = pathlib.Path(__file__).parents[1] / "static/violations/mapfilter.js"
    out = subprocess.run(["node", "-e", f"const F=require({json.dumps(str(js))});console.log(F.labelText({{address:'1 A St',lot:'4',owner:'Pat Lee'}},{{address:true,lot:false,name:true}}))"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "1 A St · Pat Lee"


def _case(owner, vtype, **kw):
    from violations.models import Violation, Photo
    from violations import services
    v = Violation.objects.create(owner=owner, vtype=vtype, description="x", cure_required="Fix it.", **kw)
    return v


def test_zero_cure_days_letter_says_immediately(db):
    from violations.models import Owner, ViolationType
    from violations.pdf import render_notice_pdf
    from violations.models import Notice
    import subprocess, tempfile, os
    o = Owner.objects.create(name="Zed Owner", property_address="1 Test Way", email="z@example.com")
    vt = ViolationType.objects.create(name="Thing", default_cure_days=15)
    v = _case(o, vt, cure_days=0, non_curable=True, board_emergency=True)
    assert v.cure_phrase == "immediately" and v.is_immediate
    n = Notice.objects.create(violation=v, kind=Notice.Kind.VIOLATION)
    pdf = render_notice_pdf(n, "owner")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf if isinstance(pdf, bytes) else pdf.getvalue())
    t = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout
    os.unlink(f.name)
    assert "cannot be cured" in t and "declared this matter an emergency" in t
    assert "within 0 days" not in t and "immediately" in t


def test_form_accepts_zero_days_and_non_curable_forces_zero(db):
    from violations.forms import ViolationForm
    from violations.models import Owner, ViolationType
    o = Owner.objects.create(name="A B", property_address="2 Test Way")
    vt = ViolationType.objects.create(name="T2")
    base = {"owner": o.pk, "vtype": vt.pk, "observed_on": "2026-01-01", "description": "d", "cure_required": "c", "cure_days": 0, "timing": "standard"}
    f = ViolationForm(base); assert f.is_valid(), f.errors
    f2 = ViolationForm({**base, "cure_days": 10, "non_curable": "on"}); assert f2.is_valid()
    assert f2.cleaned_data["cure_days"] == 0


def test_standard_issue_holds_the_mail_two_business_days_and_cure_cancels_it(db, settings, mailoutbox):
    from datetime import date
    from violations import services
    from violations.models import Owner, ViolationType, Violation, Photo, Notice
    from django.core.files.base import ContentFile
    o = Owner.objects.create(name="Hold Owner", property_address="3 Test Way", email="h@example.com")
    vt = ViolationType.objects.create(name="T3")
    v = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c")
    Photo.objects.create(violation=v, image=ContentFile(b"x", name="a.jpg"), from_resident=False)
    n = services.issue_first_notice(v)
    assert len(mailoutbox) == 1 and n.mail_held                       # emailed at once, mail waits
    assert n.mail_hold_until == services.add_business_days(date.today(), 2)
    services.mark_cured(v)
    n.refresh_from_db()
    assert n.mail_cancelled and not n.mail_held                       # fixed first: no postage


def test_urgent_mails_now_and_business_days_skip_weekends():
    from datetime import date
    from violations.services import add_business_days
    assert add_business_days(date(2026, 11, 20), 2) == date(2026, 11, 24)   # Fri + 2 business days = Tue


def test_make_test_owner_command(db, django_user_model):
    from django.core.management import call_command
    from violations.models import Owner
    call_command("make_test_owner", "--email", "me@example.com", "--address", "9 Test Lane")
    o = Owner.objects.get(property_address="9 Test Lane")
    u = django_user_model.objects.get(username="testowner")
    assert o.email == "me@example.com" and u.check_password("demo-password-123")
    assert u.profile.role == "homeowner" and u.profile.owner == o


def test_dashboard_overdue_counts(db, client, django_user_model):
    from datetime import date, timedelta
    from violations.models import Owner, ViolationType, Violation, Profile
    o = Owner.objects.create(name="O", property_address="5 Late Way")
    vt = ViolationType.objects.create(name="T5")
    Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="pending_board",
                             cure_deadline=date.today() - timedelta(days=20))
    Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="fine_approved",
                             fine_amount=50, fine_due_on=date.today() - timedelta(days=2))
    from violations.views import overdue_qs
    assert overdue_qs("board").count() == 1 and overdue_qs("fine").count() == 1 and overdue_qs("hearing").count() == 0
    u = django_user_model.objects.create_user("mg3", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    r = client.get("/violations/?overdue=fine")
    assert r.status_code == 200 and "5 Late Way" in r.content.decode()


def test_form_shows_owner_and_tenant_recipients(db, client, django_user_model):
    from datetime import date, timedelta
    from violations.models import Owner, Profile
    Owner.objects.create(name="Nilesh Patel", property_address="7213 Great Egret Boulevard #3", email="n@example.com",
                         tenant_name="Tina Tenant", tenant_email="t@example.com", tenant_end=date.today() + timedelta(days=90))
    Owner.objects.create(name="Old Lease", property_address="8 Old Way", tenant_name="Gone Tenant", tenant_end=date.today() - timedelta(days=5))
    u = django_user_model.objects.create_user("mg4", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    html = client.get("/violations/new/").content.decode()
    assert 'id="owner-info"' in html and "Tina Tenant" in html and "t@example.com" in html
    assert "will not be notified" in html


def test_draft_photo_can_be_removed_but_not_after_sending(db, client, django_user_model):
    from django.core.files.base import ContentFile
    from violations.models import Owner, ViolationType, Violation, Photo, Profile
    o = Owner.objects.create(name="P O", property_address="6 Photo Way", email="p@example.com")
    vt = ViolationType.objects.create(name="T6")
    v = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c")
    p = Photo.objects.create(violation=v, image=ContentFile(b"x", name="a.jpg"), from_resident=False)
    u = django_user_model.objects.create_user("mg5", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    assert "Remove this photo" in client.get(f"/violations/{v.pk}/").content.decode()
    client.post(f"/violations/{v.pk}/delete_photo/", {"photo": p.pk})
    assert not Photo.objects.filter(pk=p.pk).exists()
    p2 = Photo.objects.create(violation=v, image=ContentFile(b"x", name="b.jpg"), from_resident=False)
    v.status = "notice_sent"; v.save()
    client.post(f"/violations/{v.pk}/delete_photo/", {"photo": p2.pk})
    assert Photo.objects.filter(pk=p2.pk).exists()


def test_resident_pages_offer_photo_upload_with_removable_thumbnails(db, client):
    from violations.models import Owner, ViolationType, Violation
    o = Owner.objects.create(name="R O", property_address="7 Resident Way", email="r@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T7"), description="d", cure_required="c", status="notice_sent")
    from django.urls import reverse
    html = client.get(v.resident_url().replace("http://localhost:8000", "")).content.decode()
    assert 'name="photos"' in html and "photopicker.js" in html and 'id="rthumbs"' in html


def test_owner_login_command_and_portal_shows_letter_and_photo(db, client, django_user_model):
    from django.core.files.base import ContentFile
    from django.core.management import call_command
    from violations import services
    from violations.models import Owner, ViolationType, Violation, Photo
    o = Owner.objects.create(name="Adam Rouse", property_address="7316 Great Egret Boulevard #65", lot_no="65", email="a@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T8"), description="d", cure_required="c")
    Photo.objects.create(violation=v, image=ContentFile(b"x", name="a.jpg"), from_resident=False)
    services.issue_first_notice(v)
    call_command("owner_login", "--address", "7316 Great Egret")
    client.force_login(django_user_model.objects.get(username="owner-65"))
    html = client.get(f"/me/{v.pk}/").content.decode()
    assert "T8" in html and "Open / print the letter" in html and "/photos/" in html and "Send a message" in html


def test_hold_until_date_lifts_itself_restarts_cure_and_emails(db, mailoutbox):
    from datetime import date, timedelta
    from violations import services
    from violations.models import Owner, ViolationType, Violation
    o = Owner.objects.create(name="H O", property_address="10 Hold Way", email="h@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T10"), description="d", cure_required="c",
                                 status="notice_sent", cure_days=15, cure_deadline=date.today() + timedelta(days=3), extended_days=5)
    services.put_on_hold(v, "Hospital", date.today() + timedelta(days=6))
    assert services.send_hold_reminders() == 1 and "hold" in mailoutbox[-1].subject.lower()      # 6 days left: inside the 7-day window
    assert services.send_hold_reminders() == 0                                                    # not twice
    assert services.lift_expired_holds() == 0
    assert services.lift_expired_holds(date.today() + timedelta(days=6)) == 1
    v.refresh_from_db()
    assert v.status == "notice_sent" and v.off_hold_on and v.extended_days == 0
    assert v.cure_deadline == date.today() + timedelta(days=6 + 15)       # lifted 6 days from now: 15 fresh days from that day
    assert v.get_status_display().startswith("Off hold") and "Hold ended" in mailoutbox[-1].subject


def test_address_brackets_fill_in_automatically(db):
    from violations.forms import ViolationForm
    from violations.models import Owner, ViolationType
    o = Owner.objects.create(name="A B", property_address="7316 Great Egret Boulevard #65")
    vt = ViolationType.objects.create(name="Mold")
    f = ViolationForm({"owner": o.pk, "vtype": vt.pk, "observed_on": "2026-01-01", "timing": "draft", "cure_days": 7,
                       "description": "The sidewalk at [location] has mold.", "cure_required": "Clean [address]."})
    assert f.is_valid(), f.errors
    assert "7316 Great Egret Boulevard #65" in f.cleaned_data["description"] and "[" not in f.cleaned_data["cure_required"]


def test_correspondence_shows_letters_emails_and_certified_tracking(db, client, django_user_model):
    from datetime import date
    from violations import services
    from violations.models import Owner, ViolationType, Violation, Photo, Profile, Notice
    from django.core.files.base import ContentFile
    o = Owner.objects.create(name="C O", property_address="11 Corr Way", email="c@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T11"), description="d", cure_required="c")
    Photo.objects.create(violation=v, image=ContentFile(b"x", name="a.jpg"), from_resident=False)
    n = services.issue_first_notice(v, timing="urgent")
    services.mark_mailed(n, date.today(), "9407123456", method="certified")
    assert v.emails.filter(notice=n).count() == 1                       # a copy of the email is kept
    u = django_user_model.objects.create_user("mg6", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    html = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Certified" in html and "9407123456" in html and "View letter" in html and "Email copy" in html
    page = client.get(f"/owners/{o.pk}/correspondence/").content.decode()
    assert "9407123456" in page


def test_owner_home_lists_cases_nearest_cure_date_first_with_photo(db, client, django_user_model):
    from datetime import date, timedelta
    from django.core.files.base import ContentFile
    from violations.models import Owner, ViolationType, Violation, Photo, Profile
    o = Owner.objects.create(name="Sorted Owner", property_address="12 Sort Way")
    vt = ViolationType.objects.create(name="Thing")
    later = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="LaterOne"), description="d", cure_required="c", status="notice_sent", cure_deadline=date.today() + timedelta(days=20))
    sooner = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="SoonerOne"), description="d", cure_required="c", status="notice_sent", cure_deadline=date.today() + timedelta(days=2))
    Photo.objects.create(violation=sooner, image=ContentFile(b"x", name="a.jpg"), from_resident=False)
    u = django_user_model.objects.create_user("own2", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": o})
    client.force_login(u)
    html = client.get("/me/violations/").content.decode()
    assert html.index("SoonerOne") < html.index("LaterOne")
    assert "steps" not in html.split("<main")[1]                          # no stage tracker on the home screen
    assert "background-image" in html and "Cure by" in html


def test_canned_replies_defaults_insert_and_team_can_add(db, client, django_user_model):
    from violations.models import CannedReply, Owner, ViolationType, Violation, Profile
    assert CannedReply.objects.filter(title__startswith="Reply received").exists() and CannedReply.objects.count() >= 4
    o = Owner.objects.create(name="Q O", property_address="13 Reply Way", email="q@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T13"), description="d", cure_required="c", status="notice_sent")
    u = django_user_model.objects.create_user("mg7", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    assert "We will verify the correction at our next inspection" in client.get(f"/violations/{v.pk}/").content.decode()
    client.post(f"/violations/{v.pk}/reply/", {"message": "Please send a photo of the fence.", "save_reply": "on", "save_title": "Fence photo"})
    assert CannedReply.objects.filter(title="Fence photo", body__contains="fence").exists()
    client.post("/replies/", {"action": "add", "title": "Hello", "body": "Hi there"})
    assert CannedReply.objects.filter(title="Hello").exists()
    r = CannedReply.objects.get(title="Hello")
    client.post("/replies/", {"action": "update", "id": r.pk, "title": "Hello 2", "body": "Hi"})
    client.post("/replies/", {"action": "delete", "id": r.pk})
    assert not CannedReply.objects.filter(pk=r.pk).exists()


def test_committee_decision_sends_letter_3_and_rejection_notifies_owner(db, mailoutbox):
    from datetime import date, timedelta
    from django.utils import timezone
    from violations import services
    from violations.models import Owner, ViolationType, Violation
    o = Owner.objects.create(name="D O", property_address="14 Dec Way", email="d@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T14"), description="d", cure_required="c", status="hearing_scheduled",
                                 proposed_fine=100, hearing_date=timezone.now() - timedelta(hours=2))
    n = services.committee_decide(v, 1, 2, findings="No violation found.")
    v.refresh_from_db()
    assert v.status == "fine_rejected" and n.kind == "fine" and "committee decision" in mailoutbox[-1].subject.lower()
    assert v.committee_findings == "No violation found."


def states(v):
    return {s["title"]: s["state"] for s in v.progress}


def test_tracker_marks_bypassed_steps_and_shows_the_legal_stage(db):
    from violations.models import Owner, ViolationType, Violation
    o = Owner.objects.create(name="L O", property_address="15 Legal Way")
    vt = ViolationType.objects.create(name="T15")
    legal = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="referred_legal")
    st = states(legal)
    assert st["Fine issued by Board"] == "skipped" and st["Violation sent to Compliance Committee / Homeowner Hearing"] == "skipped"
    assert st["Fine Imposed & Posted to Ledger"] == "skipped" and st["Referred to the attorney"] == "current" and st["Closed"] == "todo"
    assert st["Notice issued"] == "done" and st["Cure period"] == "done"
    cured = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="cured")
    assert [s["state"] for s in cured.progress] == ["done", "done", "skipped", "skipped", "skipped", "done"]
    normal = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="pending_board")
    assert [s["state"] for s in normal.progress] == ["done", "done", "current", "todo", "todo", "todo"]
    sched = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="hearing_scheduled")
    assert [s["state"] for s in sched.progress] == ["done", "done", "done", "current", "todo", "todo"]


def test_tracker_boxes_carry_icons_dates_and_an_owner_sentence(db):
    from datetime import date
    from violations.models import Owner, ViolationType, Violation
    o = Owner.objects.create(name="T O", property_address="16 Track Way")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T16"), description="d", cure_required="c",
                                 status="notice_sent", cure_deadline=date(2026, 12, 18))
    t = v.progress
    assert [s["title"] for s in t] == ["Notice issued", "Cure period", "Fine issued by Board",
                                       "Violation sent to Compliance Committee / Homeowner Hearing", "Fine Imposed & Posted to Ledger", "Closed"]
    assert t[0]["icon"] and t[1]["date"] == date(2026, 12, 18) and t[1]["state"] == "current"
    assert t[1]["text"] == "Please correct this by Dec 18, 2026. You can reply, upload a photo of the fix, or ask for more time."
    v.status = "fine_approved"; v.fine_amount = 100; v.fine_due_on = date(2027, 1, 5); v.save()
    assert dict((s["title"], s["text"]) for s in v.progress)["Fine Imposed & Posted to Ledger"] == "A fine of $100.00 was imposed, due Jan 5, 2027. Please see your decision letter."
    legal = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T17"), description="d", cure_required="c", status="referred_legal")
    assert any(s["state"] == "current" and s["icon"] == "\u2696" for s in legal.progress)
    assert any(s["state"] == "skipped" and s["date"] is None for s in legal.progress)


def test_staff_steps_have_sub_steps_for_the_current_stage(db):
    from datetime import date, timedelta
    from violations.models import Owner, ViolationType, Violation, Notice, Response
    o = Owner.objects.create(name="S O", property_address="17 Sub Way")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T18b"), description="d", cure_required="c", status="notice_sent",
                                 cure_deadline=date.today() + timedelta(days=5), extended_days=3, reinspect_on=date.today())
    Notice.objects.create(violation=v, kind="violation", mail_hold_until=date.today() + timedelta(days=2))
    Response.objects.create(violation=v, message="fixed", from_resident=True)
    subs = {s["key"]: {u["label"]: u["state"] for u in s["subs"]} for s in v.staff_progress}
    assert subs["notice"]["US Mail copy"] == "current"
    assert subs["cure"]["Owner replied"] == "done" and subs["cure"]["Extension granted"] == "done" and subs["cure"]["Re-inspect"] == "current"
    assert subs["board"]["Waiting for Board vote"] == "todo"


def test_reinspect_flag_ages_shows_in_lists_and_resolves(db, client, django_user_model):
    from datetime import date, timedelta
    from violations import services
    from violations.models import Owner, ViolationType, Violation, Profile, Response
    o = Owner.objects.create(name="R I", property_address="18 Recheck Way", email="r@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T18"), description="d", cure_required="c", status="notice_sent",
                                 cure_deadline=date.today() + timedelta(days=5))
    Response.objects.create(violation=v, message="It's fixed!", from_resident=True, cure_reported=True)
    services.mark_reinspect(v)
    v.refresh_from_db()
    assert v.get_status_display() == "Re-inspect" and v.needs_reinspection and v.status == "notice_sent"
    assert not v.responses.filter(cure_reported=True, handled=False).exists()
    assert v.responses.filter(from_resident=False, message__contains="verify the correction").exists()
    with __import__("pytest").raises(services.WorkflowError):
        services.mark_reinspect(v)
    v.reinspect_on = date.today() - timedelta(days=9); v.save()
    assert v.reinspect_age == 9
    u = django_user_model.objects.create_user("mg8", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    assert "18 Recheck Way" in client.get("/board/").content.decode()                 # on the Board queue's verify list
    assert "9 days" in client.get("/").content.decode()
    services.reinspect_result(v, corrected=False, note="Weeds remain.")
    v.refresh_from_db()
    assert not v.needs_reinspection and v.status == "notice_sent"
    services.mark_reinspect(v, notify=False)
    services.reinspect_result(v, corrected=True)
    v.refresh_from_db()
    assert v.status == "cured" and v.reinspect_on is None


def _legal_case(status="fine_approved", **kw):
    from datetime import date, timedelta
    from django.core.files.base import ContentFile
    from violations.models import Owner, ViolationType, Violation, Photo, Notice
    o = Owner.objects.create(name="Lee Owner", property_address="19 Legal Ln", lot_no="19", email="l@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="Fence without approval", section="3.22", standards_title="Fences"),
                                 description="A fence was installed.", cure_required="Remove it.", status=status, **kw)
    Photo.objects.create(violation=v, image=ContentFile(b"jpgdata", name="fence.jpg"), from_resident=False)
    Notice.objects.create(violation=v, kind="violation")
    return v


def test_send_to_legal_drafts_an_email_and_sends_nothing_until_clicked(db, client, django_user_model, mailoutbox, settings):
    from datetime import date
    from violations import services
    from violations.models import Profile
    settings.ATTORNEY_EMAIL, settings.ATTORNEY_NAME = "counsel@lawfirm.test", "Ms. Counsel"
    v = _legal_case(fine_amount=200, fine_due_on=date(2026, 12, 20), committee_votes_for=3, committee_votes_against=0, board_votes_for=3, board_votes_against=0)
    u = django_user_model.objects.create_user("mg9", password="x", email="mgr@example.com")
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    r = client.post(f"/violations/{v.pk}/legal/", {"note": ""}, follow=True)
    v.refresh_from_db()
    assert v.status == "referred_legal" and v.legal_path == "after_fine" and not mailoutbox      # referred, nothing sent
    html = r.content.decode()
    assert "Not sent" in html and "counsel@lawfirm.test" in html and "Ms. Counsel" in html
    assert "issued a fine of $200.00" in html and "the violation is not complete" in html and "Violation photo:" in html
    keys = [a["key"] for a in services.legal_email_draft(v)["attachments"]]
    r = client.post(f"/violations/{v.pk}/legal/email/", {"do": "send", "to": "counsel@lawfirm.test", "subject": "Referral", "body": "Please review.", "att": keys})
    assert len(mailoutbox) == 1 and mailoutbox[0].to == ["counsel@lawfirm.test"] and len(mailoutbox[0].attachments) == len(keys) == 2
    v.refresh_from_db()
    assert v.legal_sent_on and v.legal_sent_to == "counsel@lawfirm.test"
    assert not v.emails.exists()                                                                   # not in the owner's correspondence


def test_direct_referral_needs_a_reason_and_the_email_says_no_fine(db, client, django_user_model, mailoutbox):
    from violations import services
    from violations.models import Profile
    v = _legal_case(status="notice_sent", safety_issue=True)
    u = django_user_model.objects.create_user("mg10", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    client.post(f"/violations/{v.pk}/legal/", {"note": ""})
    v.refresh_from_db(); assert v.status == "notice_sent"                                           # a reason is required
    client.post(f"/violations/{v.pk}/legal/", {"note": "Structural hazard"})
    v.refresh_from_db(); assert v.status == "referred_legal" and v.legal_path == "direct" and not mailoutbox
    body = services.legal_email_draft(v)["body"]
    assert "referring it directly" in body and "no fine has been imposed" in body and "Structural hazard" in body and "safety issue" in body
    st = states(v)
    assert st["Fine issued by Board"] == "skipped" and st["Referred to the attorney"] == "current"
    z = client.post(f"/violations/{v.pk}/legal/email/", {"do": "download", "to": "a@b.c", "subject": "s", "body": "b", "att": ["photo-1"]})
    assert z["Content-Type"] == "application/zip"


def _meeting_with(n, django_user_model, role="committee"):
    from datetime import timedelta
    from django.utils import timezone
    from violations.models import Owner, ViolationType, Violation, CommitteeMeeting, Profile
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=3), zoom_link="https://z.us/9")
    vt = ViolationType.objects.create(name="Mailbox", default_fine=100)
    cases = []
    for i in range(n):
        o = Owner.objects.create(name=f"Own {i}", property_address=f"{i + 1} Meeting Way", email=f"o{i}@example.com")
        cases.append(Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="hearing_scheduled",
                                              committee_meeting=m, hearing_date=m.starts_at, proposed_fine=100))
    u = django_user_model.objects.create_user("cm1", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": role})
    return m, cases, u


def test_committee_works_the_list_then_board_reviews_every_item(db, client, django_user_model, mailoutbox):
    from violations import services
    from violations.models import Profile
    m, (a, b, c, d), cu = _meeting_with(4, django_user_model)
    client.force_login(cu)
    assert client.get(f"/committee/meetings/{m.pk}/").status_code == 200
    # decisions saved on the case; nothing moves yet
    client.post(f"/committee/cases/{a.pk}/record/", {"outcome": "confirm", "votes_for": 3, "votes_against": 0, "findings": "Seen."})
    client.post(f"/committee/cases/{b.pk}/record/", {"outcome": "amend", "amount": "60", "votes_for": 2, "votes_against": 1, "findings": "Partly fixed."})
    client.post(f"/committee/cases/{c.pk}/record/", {"outcome": "waive", "findings": "Owner fixed it before the meeting."})
    client.post(f"/committee/meetings/{m.pk}/submit/")                                   # d is undecided: refused
    m.refresh_from_db(); assert m.results_status == "open"
    client.post(f"/committee/cases/{d.pk}/record/", {"outcome": "reject", "votes_for": 1, "votes_against": 2, "findings": "No violation."})
    client.post(f"/committee/meetings/{m.pk}/save/", {"attendees": "Ann\nBob\nCy", "notes": "Went well"})
    client.post(f"/committee/meetings/{m.pk}/submit/")
    m.refresh_from_db(); a.refresh_from_db()
    assert m.results_status == "submitted" and m.attendees.startswith("Ann") and a.status == "ledger_pending" and a.fine_amount == 100
    assert not mailoutbox                                                                # nothing imposed or sent before Board review
    bu = django_user_model.objects.create_user("bd1", password="x"); Profile.objects.update_or_create(user=bu, defaults={"role": "board"})
    client.force_login(bu)
    assert "Own 0" in client.get(f"/results/{m.pk}/").content.decode()
    assert "1 to review" in client.get("/").content.decode() or "to review" in client.get("/").content.decode()
    client.post(f"/results/cases/{a.pk}/review/", {"action": "approve"})
    client.post(f"/results/cases/{b.pk}/review/", {"action": "modify", "amount": "40"})
    client.post(f"/results/cases/{c.pk}/review/", {"action": "approve"})
    client.post(f"/results/cases/{d.pk}/review/", {"action": "return", "note": "Please re-check the photo."})
    for v in (a, b, c, d): v.refresh_from_db()
    assert a.status == "fine_approved" and a.fine_amount == 100 and not a.board_modified
    assert b.status == "fine_approved" and b.fine_amount == 40 and b.board_modified and b.board_outcome == "amend"
    assert c.status == "waived" and d.status == "hearing_scheduled" and d.committee_outcome == ""
    m.refresh_from_db(); assert m.results_status == "open"                                # returned item reopens the meeting
    subjects = " | ".join(x.subject.lower() for x in mailoutbox)
    assert "committee decision" in subjects and "waived" in subjects
    results = client.get("/results/").content.decode()
    assert m.starts_at.strftime("%B") in results


def test_board_cannot_raise_a_fine_above_the_hearing_notice(db, django_user_model):
    import pytest
    from violations import services
    m, (a,), cu = _meeting_with(1, django_user_model)
    services.committee_record(a, "confirm", cu, votes_for=3, votes_against=0, findings="ok")
    services.submit_meeting_results(m, cu)
    a.refresh_from_db()
    with pytest.raises(services.WorkflowError):
        services.board_review_item(a, cu, "modify", amount="500")
    with pytest.raises(services.WorkflowError):
        services.committee_record(a, "waive", cu, findings="late")                       # already submitted


def test_new_violation_page_script_runs_without_errors_and_search_lists_weeds(db, client, django_user_model):
    """Guards against a script error (it once stopped the violation search box from working)."""
    import shutil, subprocess, re, tempfile, os, json
    from django.core.management import call_command
    from violations.models import Owner, Profile
    if not shutil.which("node"):
        import pytest; pytest.skip("node not installed")
    call_command("load_standards")
    Owner.objects.create(name="Pat", property_address="1 Js Way", email="p@example.com")
    u = django_user_model.objects.create_user("mg11", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    html = client.get("/violations/new/").content.decode()
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    main = max(scripts, key=len)
    stub = ("const els={};const mk=()=>({value:'',style:{},textContent:'',innerHTML:'',classList:{add(){},remove(){}},addEventListener(){},dispatchEvent(){},"
            "setAttribute(){},appendChild(){},insertBefore(){},querySelector:()=>mk(),children:[],options:[],selectedIndex:0,parentNode:{insertBefore(){}},scrollIntoView(){},focus(){}});"
            "global.document={getElementById:(id)=>{if(id==='catalog-data')return{textContent:'[]'};if(id==='owner-info')return{textContent:'{}'};return els[id]||(els[id]=mk())},createElement:()=>mk(),querySelectorAll:()=>[]};"
            "global.fetch=()=>Promise.resolve({json:()=>({results:[]})});global.DataTransfer=function(){this.items={add(){}};this.files=[]};global.Event=function(){};global.URL={createObjectURL:()=>''};")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(stub + main)
    r = subprocess.run(["node", f.name], capture_output=True, text=True)
    os.unlink(f.name)
    assert r.returncode == 0, r.stderr[-400:]


def test_committee_sees_cases_after_the_board_vote_and_login_command(db, client, django_user_model):
    from django.core.management import call_command
    from violations.models import Owner, ViolationType, Violation
    call_command("make_login", "committee", "--username", "cmtest", "--name", "Casey Member")
    o = Owner.objects.create(name="W O", property_address="20 Wait Way")
    vt = ViolationType.objects.create(name="Trash")
    waiting = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="hearing_needed", proposed_fine=100)
    early = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="pending_board")
    client.force_login(django_user_model.objects.get(username="cmtest"))
    html = client.get("/committee/").content.decode()
    assert "Waiting to be scheduled" in html and "20 Wait Way" in html
    assert client.get(f"/committee/cases/{waiting.pk}/").status_code == 200           # visible once the Board has voted
    assert client.get(f"/committee/cases/{early.pk}/").status_code == 403             # not before


def test_backdate_meeting_lets_the_committee_decide_now(db, django_user_model):
    from django.core.management import call_command
    from violations import services
    m, (a,), cu = _meeting_with(1, django_user_model)
    from datetime import timedelta
    from django.utils import timezone
    m.starts_at = timezone.now() + timedelta(days=20); m.save(); a.hearing_date = m.starts_at; a.save()
    import pytest
    with pytest.raises(services.WorkflowError):
        services.committee_record(a, "confirm", cu, votes_for=3, votes_against=0, findings="x")
    call_command("backdate_meeting", str(m.pk))
    a.refresh_from_db()
    services.committee_record(a, "confirm", cu, votes_for=3, votes_against=0, findings="x")


def test_refer_to_board_early_asks_for_confirmation(db, client, django_user_model):
    from violations.models import Owner, ViolationType, Violation, Profile
    o = Owner.objects.create(name="E O", property_address="21 Early Way")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T21"), description="d", cure_required="c", status="notice_sent")
    u = django_user_model.objects.create_user("mg12", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    html = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Refer this case to the Board early?" in html and "This is unusual" in html


def test_owner_portal_status_pills_are_color_coded_with_friendly_wording(db, client, django_user_model):
    from datetime import date, timedelta
    from violations.models import Owner, ViolationType, Violation, Profile
    o = Owner.objects.create(name="C P", property_address="22 Color Way")
    vt = ViolationType.objects.create(name="T22")
    a = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="notice_sent", cure_deadline=date.today() + timedelta(days=5))
    b = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T23"), description="d", cure_required="c", status="pending_board", cure_deadline=date.today() - timedelta(days=1))
    u = django_user_model.objects.create_user("own3", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": o})
    client.force_login(u)
    html = client.get("/me/violations/").content.decode()
    assert 'class="pill yellow">In Cure Period' in html and 'class="pill orange">Sent to Board for Fine' in html
    assert "Cure period running" not in html and "Awaiting board vote" not in html
    assert "In Cure Period" in client.get(f"/me/{a.pk}/").content.decode()


def test_board_queue_bulk_decisions_with_per_row_overrides(db, client, django_user_model):
    from datetime import date, timedelta
    from violations.models import Owner, ViolationType, Violation, Profile
    vt = ViolationType.objects.create(name="Bulk", default_fine=100)
    cases = []
    for i in range(3):
        o = Owner.objects.create(name=f"B{i}", property_address=f"{i + 30} Bulk Way")
        cases.append(Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="pending_board",
                                              cure_deadline=date.today() - timedelta(days=20)))
    u = django_user_model.objects.create_user("bd2", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    page = client.get("/board/").content.decode()
    assert 'id="all"' in page and "Fine issued" in page and "Suspension" not in page and "votes" not in page.lower().replace("by majority vote", "")
    d = date.today().isoformat()
    a, b, c = cases
    post = {"pick": [a.pk, b.pk, c.pk], "date": d, "note": "",
            f"decision_{a.pk}": "proceed", f"fine_{a.pk}": "100", f"date_{a.pk}": d,
            f"decision_{b.pk}": "proceed", f"fine_{b.pk}": "75", f"date_{b.pk}": d,          # a different fine for one row
            f"decision_{c.pk}": "dismiss", f"date_{c.pk}": d}
    client.post("/board/bulk/", post)                                                          # no majority tick: nothing happens
    a.refresh_from_db(); assert a.status == "pending_board"
    client.post("/board/bulk/", {**post, "by_majority": "on"})
    for v in cases: v.refresh_from_db()
    assert (a.status, a.proposed_fine) == ("hearing_needed", 100) and (b.status, b.proposed_fine) == ("hearing_needed", 75) and c.status == "dismissed"
    assert a.board_votes_for is None and a.proposed_suspension == ""


def test_sent_to_compliance_committee_pill_is_red_in_the_owner_portal(db):
    from violations.models import Owner, ViolationType, Violation
    o = Owner.objects.create(name="R P", property_address="24 Red Way")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T24"), description="d", cure_required="c", status="hearing_scheduled")
    assert v.portal_status == ("Sent to Compliance Committee", "red")


def test_owner_home_shows_when_the_cure_period_ended_for_later_stages(db, client, django_user_model):
    from datetime import date
    from violations.models import Owner, ViolationType, Violation, Profile
    o = Owner.objects.create(name="E D", property_address="25 Ended Way")
    Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T25"), description="d", cure_required="c", status="hearing_scheduled",
                             cure_deadline=date(2026, 11, 3))
    u = django_user_model.objects.create_user("own4", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": o})
    client.force_login(u)
    html = client.get("/me/violations/").content.decode()
    assert "Cure period ended" in html and "Nov 3, 2026" in html


def test_point_in_time_goes_straight_to_the_board_queue_and_stands_out(db, client, django_user_model, mailoutbox):
    from violations import services
    from violations.models import Owner, ViolationType, Violation, Photo, Profile
    from django.core.files.base import ContentFile
    o = Owner.objects.create(name="P I", property_address="26 Point Way", email="p@example.com")
    vt = ViolationType.objects.create(name="Dog off leash")
    old = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="closed", non_curable=True)
    v = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", non_curable=True, cure_days=0)
    Photo.objects.create(violation=v, image=ContentFile(b"x", name="a.jpg"), from_resident=False)
    services.issue_first_notice(v)
    v.refresh_from_db()
    assert v.status == "pending_board" and mailoutbox                                         # notice emailed, then straight to the Board
    assert states(v)["Cure period"] == "skipped"
    assert services.owner_history(v)["pit_count"] == 1
    u = django_user_model.objects.create_user("bd3", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    page = client.get("/board/").content.decode()
    assert "Point in time" in page and "1</b> prior point-in-time" in page
    cu = django_user_model.objects.create_user("cm2", password="x"); Profile.objects.update_or_create(user=cu, defaults={"role": "committee"})
    v.status = "hearing_needed"; v.save()
    client.force_login(cu)
    assert "1 prior point-in-time violation" in client.get(f"/committee/cases/{v.pk}/").content.decode()


def test_owner_portal_links_to_hoa_documents(db, client, django_user_model):
    from violations.models import Owner, Profile
    o = Owner.objects.create(name="D L", property_address="27 Docs Way")
    u = django_user_model.objects.create_user("own5", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": o})
    client.force_login(u)
    html = client.get("/me/violations/").content.decode()
    assert "https://home.sunstatemanagement.com/login" in html and "HOA Documents" in html


def test_owner_pages_no_longer_offer_the_bringing_an_attorney_checkbox(db, client, django_user_model):
    from violations.models import Owner, ViolationType, Violation, Profile
    o = Owner.objects.create(name="N A", property_address="28 Counsel Way")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T28"), description="d", cure_required="c", status="hearing_scheduled")
    u = django_user_model.objects.create_user("own6", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": o})
    client.force_login(u)
    assert "bring an attorney" not in client.get(f"/me/{v.pk}/").content.decode()
    assert "bring an attorney" not in client.get(f"/r/{v.token}/").content.decode()


def test_unread_owner_message_shows_an_envelope_until_staff_open_the_case(db, client, django_user_model):
    from violations.models import Owner, ViolationType, Violation, Profile, Response
    o = Owner.objects.create(name="M O", property_address="29 Mail Way")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T29"), description="d", cure_required="c", status="notice_sent")
    Response.objects.create(violation=v, message="Hello", from_resident=True)
    u = django_user_model.objects.create_user("mg13", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    assert "Unread message from the owner" in client.get("/violations/").content.decode()
    client.get(f"/violations/{v.pk}/")
    assert "Unread message from the owner" not in client.get("/violations/").content.decode()


def test_clear_test_data_needs_confirmation_and_keeps_owners(db):
    import pytest
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from violations.models import Owner, ViolationType, Violation
    o = Owner.objects.create(name="K O", property_address="30 Keep Way")
    Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T30"), description="d", cure_required="c")
    with pytest.raises(CommandError):
        call_command("clear_test_data")
    assert Violation.objects.count() == 1
    call_command("clear_test_data", "--yes")
    assert Violation.objects.count() == 0 and Owner.objects.filter(pk=o.pk).exists() and ViolationType.objects.exists()


def test_violations_are_in_the_admin(db, client, django_user_model):
    u = django_user_model.objects.create_superuser("root1", "r@example.com", "x")
    client.force_login(u)
    assert client.get("/admin/violations/violation/").status_code == 200


def test_owner_front_page_has_four_tiles_with_status_icons(db, client, django_user_model):
    from datetime import date, timedelta
    from violations.models import Owner, ViolationType, Violation, Profile
    o = Owner.objects.create(name="Tile Owner", property_address="31 Tile Way", email="t@example.com")
    u = django_user_model.objects.create_user("own7", password="x", first_name="Tilly")
    Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": o})
    client.force_login(u)
    assert client.get("/").url == "/me/"
    html = client.get("/me/").content.decode()
    assert html.count('class="tilebox') == 4 and "Welcome, Tilly" in html
    for title in ("Violations", "Account details", "HOA documents", "Sign-in options"):
        assert title in html
    assert "All clear" in html and 'tilebox green' in html                                   # nothing open
    vt = ViolationType.objects.create(name="Tile type")
    v = Violation.objects.create(owner=o, vtype=vt, description="d", cure_required="c", status="notice_sent", cure_deadline=date.today() + timedelta(days=20))
    html = client.get("/me/").content.decode()
    assert "In cure period" in html and "1 open" in html and "tilebox yellow" in html
    v.cure_deadline = date.today() + timedelta(days=2); v.save()
    assert "Due in 2 days" in client.get("/me/").content.decode()
    v.status = "hearing_scheduled"; v.save()
    assert "With the Compliance Committee" in client.get("/me/").content.decode() and "tilebox red" in client.get("/me/").content.decode()
    assert client.get("/me/violations/").status_code == 200


def test_owner_violations_page_has_a_closed_toggle_and_groups_by_the_owner_definition(db, client, django_user_model):
    from datetime import date, timedelta
    from violations.models import Owner, ViolationType, Violation, Profile
    o = Owner.objects.create(name="C T", property_address="32 Closed Way")
    vt = ViolationType.objects.create(name="T32")
    Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="OpenOne"), description="d", cure_required="c", status="notice_sent", cure_deadline=date.today() + timedelta(days=9))
    for i, st in enumerate(["cured", "closed", "dismissed", "fine_rejected", "waived", "fine_approved"]):
        Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name=f"Done{i}"), description="d", cure_required="c", status=st,
                                 fine_amount=100 if st == "fine_approved" else None, fine_due_on=date.today() + timedelta(days=30))
    u = django_user_model.objects.create_user("own9", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "homeowner", "owner": o})
    client.force_login(u)
    html = client.get("/me/violations/").content.decode()
    assert 'id="show-closed"' in html and "Show closed violations" in html and 'id="closed-section"' in html
    assert html.count('class="vcard muted"') == 6 and html.count('class="vcard"') == 1                  # an imposed fine counts as closed
    assert "1 open" in client.get("/me/").content.decode()
    # only imposed fine left: the tile still tells the owner there is a fine to pay
    Violation.objects.filter(status="notice_sent").delete()
    home = client.get("/me/").content.decode()
    assert "1 fine to pay" in home and "Fine due" in home and "All clear" not in home


def test_manager_and_board_have_identical_access(db, client, django_user_model):
    from violations.models import Profile
    pages = ["/", "/violations/", "/board/", "/results/", "/import/", "/map/", "/replies/", "/admin/"]
    seen = {}
    for role in ("manager", "board"):
        u = django_user_model.objects.create_user(f"same-{role}", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": role})
        u.refresh_from_db()
        assert u.is_staff and u.is_superuser                                              # the same admin access too
        client.force_login(u)
        seen[role] = [client.get(p, follow=True).status_code for p in pages]
    assert seen["manager"] == seen["board"] == [200] * len(pages)
