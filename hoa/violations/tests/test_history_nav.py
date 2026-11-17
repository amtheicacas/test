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
    assert "2nd" in page and "time for this violation type" in page and "$100 in prior fines" in page


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
    assert nav(client.get("/me/").content.decode()) == {"My violations": "1 open"}


def test_logged_out_pages_have_no_menu(client):
    assert 'class="nav"' not in client.get("/accounts/login/").content.decode()
