"""Every route, every kind of login: who can open what. A change that opens a staff page to the wrong person, or makes any page crash for any login,
fails here. (Built during the stabilisation pass from a full probe of the routes.)"""
import hashlib
import io
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from violations import sitevisits
from violations.models import CommitteeMeeting, CommitteeMember, MinutesFile, Notice, Owner, Photo, Profile, SentLetter, SiteVisitPhoto, Violation, ViolationType

pytestmark = pytest.mark.django_db
U = get_user_model()
S = Violation.Status

STAFF_ONLY = """committee_vote dashboard violation_list violation_create standards_search violation_detail canned_replies board_bulk legal_refer legal_email
owner_correspondence case_file sent_letter violation_action notice_mailed board_queue meetings meeting_manage meeting_assign_selected
meeting_assign_all owner_archive owner_logins staff_logins audit_log results_list results_detail results_review import_owners import_template
locate_homes map map_api""".split()
COMMITTEE_OR_STAFF = """committee_home committee_meeting committee_meeting_save committee_submit meeting_minutes committee_members committee_minutes
minutes_approve committee_case_record committee_case site_visit_add site_visit_photo""".split()
OWNER_PAGES = "my_home my_violations my_hearing my_violation my_violation_state my_household my_password".split()
KIND = {}
for n in "violation_detail legal_refer legal_email case_file site_visit_add violation_action committee_case_record results_review committee_case committee_vote my_violation my_violation_state".split():
    KIND[n] = "violation"
for n in "owner_correspondence owner_logins".split():
    KIND[n] = "owner"
KIND.update(sent_letter="letter", site_visit_photo="svphoto", notice_mailed="notice", notice_pdf="notice", photo="photo")
for n in "meeting_manage meeting_assign_all committee_meeting committee_meeting_save committee_submit meeting_minutes committee_minutes minutes_approve results_detail my_hearing".split():
    KIND[n] = "meeting"


def mk(name, role, owner=None):
    u = U.objects.create_user(name, f"{name}@example.com", "x", first_name=name.title(), last_name="T")
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


@pytest.fixture
def world():
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=20), zoom_link="https://z.example/1")
    oa = Owner.objects.create(name="Alex", property_address="1 A Way", email="a@example.com")
    ob = Owner.objects.create(name="Bea", property_address="2 B Way", email="b@example.com")
    t = ViolationType.objects.create(name="T", default_cure_days=15)
    va = Violation.objects.create(owner=oa, vtype=t, description="d", cure_required="c", status=S.HEARING_SCHEDULED, committee_meeting=m, hearing_date=m.starts_at)
    img = io.BytesIO(); Image.new("RGB", (20, 20), "red").save(img, "JPEG")
    ph = Photo(violation=va); ph.image.save("p.jpg", ContentFile(img.getvalue()), save=True)
    n = Notice.objects.create(violation=va, kind="violation")
    data = b"%PDF-1.4 x"
    sl = SentLetter(violation=va, notice=n, kind="violation", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    sl.file.save("l.pdf", ContentFile(data), save=False); sl.save()
    board, mgr, com = mk("board1", "board"), mk("mgr1", "manager"), mk("comm1", "committee")
    CommitteeMember.objects.create(name="Comm", year=timezone.localdate().year, user=com)
    ha, hb = mk("homea", "homeowner", oa), mk("homeb", "homeowner", ob)
    sitevisits.add(board, va, "note", [SimpleUploadedFile("s.jpg", img.getvalue(), content_type="image/jpeg")])
    mf = MinutesFile(meeting=m, kind="pdf", sha256="0" * 64)
    mf.file.save("m.pdf", ContentFile(data), save=False); mf.save()
    ids = {"violation": va.pk, "owner": oa.pk, "letter": sl.pk, "svphoto": SiteVisitPhoto.objects.first().pk, "notice": n.pk, "meeting": m.pk, "photo": ph.pk}
    return dict(ids=ids, users={"anon": None, "homeA": ha, "homeB": hb, "committee": com, "board": board, "manager": mgr})


def url_for(name, ids):
    from violations import urls as vu
    p = next(p for p in vu.urlpatterns if p.name == name)
    kw = {}
    if "<int:pk>" in str(p.pattern):
        kw["pk"] = ids.get(KIND.get(name), 1)
    if "<str:action>" in str(p.pattern):
        kw["action"] = "note"
    return reverse(name, kwargs=kw)


def hit(user, url, post):
    c = Client(raise_request_exception=False)
    if user:
        c.force_login(user)
    return (c.post(url, {}) if post else c.get(url)).status_code


@pytest.mark.parametrize("post", [False, True])
@pytest.mark.parametrize("name", STAFF_ONLY)
def test_staff_pages_are_closed_to_everyone_but_board_and_manager(world, name, post):
    url = url_for(name, world["ids"])
    for who in ("anon", "homeA", "homeB", "committee"):
        assert hit(world["users"][who], url, post) in (302, 403, 404, 405), (name, who, post)
    for who in ("board", "manager"):
        assert hit(world["users"][who], url, post) < 500, (name, who, post)


@pytest.mark.parametrize("post", [False, True])
@pytest.mark.parametrize("name", COMMITTEE_OR_STAFF)
def test_committee_pages_are_closed_to_owners_and_visitors(world, name, post):
    url = url_for(name, world["ids"])
    for who in ("anon", "homeA", "homeB"):
        assert hit(world["users"][who], url, post) in (302, 403, 404, 405), (name, who, post)    # even the owner of the very case
    for who in ("committee", "board", "manager"):
        assert hit(world["users"][who], url, post) < 500, (name, who, post)


@pytest.mark.parametrize("name", OWNER_PAGES)
def test_owner_pages_are_for_owners_only_and_never_show_someone_elses_case(world, name):
    url = url_for(name, world["ids"])
    assert hit(None, url, False) == 302
    for who in ("committee", "board", "manager"):                       # staff with no owner record of their own
        assert hit(world["users"][who], url, False) == 403, (name, who)
    other = hit(world["users"]["homeB"], url, False)
    assert other in (200, 404, 405) and (name not in ("my_violation", "my_violation_state", "my_hearing") or other == 404), (name, other)


def test_a_notice_pdf_is_only_for_the_owner_the_committee_and_staff(world):
    url = url_for("notice_pdf", world["ids"])
    assert hit(None, url, False) == 302 and hit(world["users"]["homeB"], url, False) == 403
    assert hit(world["users"]["homeA"], url, False) == 200 and hit(world["users"]["board"], url, False) == 200


def test_a_photo_is_only_for_those_who_may_see_the_case_and_others_get_the_same_answer_as_a_missing_photo(world):
    url = url_for("photo", world["ids"])
    assert hit(None, url, False) == 302 and hit(world["users"]["homeB"], url, False) == 404
    assert hit(world["users"]["homeA"], url, False) == 200 and hit(world["users"]["committee"], url, False) == 200


def test_every_named_route_is_classified_so_a_new_one_cannot_slip_through_unchecked():
    from violations import urls as vu
    known = set(STAFF_ONLY) | set(COMMITTEE_OR_STAFF) | set(OWNER_PAGES)
    public_or_special = {"home_root", "notice_pdf", "photo", "security_setup", "security_verify", "get_access", "signin_link", "resident", "resident_photo",
                         "violation_action"}
    unclassified = [p.name for p in vu.urlpatterns if p.name not in known and p.name not in public_or_special]
    assert unclassified == [], f"add these routes to STAFF_ONLY / COMMITTEE_OR_STAFF / OWNER_PAGES in test_access_matrix.py: {unclassified}"
