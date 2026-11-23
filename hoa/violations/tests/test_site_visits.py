"""Committee site-visit observations: internal, add-only notes and photographs (committee, Board and manager only; never owners)."""
import csv
import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from violations import minutes, services, sitevisits, uploads
from violations.models import (CommitteeMeeting, CommitteeMember, Event, Notice, Owner, Profile, SiteVisitAccess, SiteVisitObservation, SiteVisitPhoto,
                               Violation, ViolationType)
from violations.pdf import render_notice_pdf
from violations.tests.review4_support import body

S = Violation.Status
A = SiteVisitAccess.Action
pytestmark = pytest.mark.django_db
U = get_user_model()
SECRET = "The gate was open and the dumpster overflowing at 8am"


def make_user(name, role, owner=None, first="Pat", last="Person"):
    u = U.objects.create_user(name, f"{name}@example.com", "x", first_name=first, last_name=last)
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


def jpeg(color="green", taken=None, size=(40, 30)):
    img = Image.new("RGB", size, color)
    b = io.BytesIO()
    if taken:
        exif = img.getexif()
        exif[306] = taken.strftime("%Y:%m:%d %H:%M:%S")
        img.save(b, "JPEG", exif=exif)
    else:
        img.save(b, "JPEG")
    return b.getvalue()


def upload(name, data, ctype="image/jpeg"):
    return SimpleUploadedFile(name, data, content_type=ctype)


@pytest.fixture
def w():
    """A case on a committee meeting, an unrelated case, and one login of every kind."""
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=3))
    oa = Owner.objects.create(name="Alex Case-Owner", property_address="1 Case Way", email="a@example.com")
    ob = Owner.objects.create(name="Bea Other", property_address="2 Other Way", email="b@example.com")
    t = ViolationType.objects.get_or_create(name="Trash cans", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=oa, vtype=t, description="d", cure_required="Put the cans away", status=S.HEARING_SCHEDULED, committee_meeting=m,
                                 hearing_date=m.starts_at, proposed_fine=100, cure_deadline=timezone.localdate() + timedelta(days=3))
    far = Violation.objects.create(owner=ob, vtype=t, description="d", cure_required="x", status=S.NOTICE_SENT)
    year = timezone.localdate().year
    out = dict(m=m, oa=oa, ob=ob, v=v, far=far)
    out["board"], out["manager"] = make_user("board1", "board", first="Bo", last="Ard"), make_user("mgr1", "manager", first="Ma", last="Nager")
    out["committee"] = make_user("comm1", "committee", first="Cam", last="Member")
    CommitteeMember.objects.create(name="Cam Member", year=year, user=out["committee"])
    out["home_a"], out["home_b"] = make_user("homea", "homeowner", oa), make_user("homeb", "homeowner", ob)
    out["dual_own"] = make_user("dualown", "committee", oa, first="Dual", last="Own")             # a committee member who is this case's owner
    CommitteeMember.objects.create(name="Dual Own", year=year, user=out["dual_own"], owner=oa)
    out["dual_other"] = make_user("dualother", "committee", ob, first="Dual", last="Other")       # committee member, owner of ANOTHER home
    CommitteeMember.objects.create(name="Dual Other", year=year, user=out["dual_other"], owner=ob)
    out["board_owner"] = make_user("boardown", "board", oa, first="Board", last="Owner")          # a Board member who owns this very violation
    return type("W", (), out)


def add_url(v):
    return f"/committee/cases/{v.pk}/observations/"


def post(client, user, v, comment=SECRET, photos=(), **extra):
    client.force_login(user)
    return client.post(add_url(v), {"comment": comment, "photos": list(photos), "back": "meeting", **extra})


# ============================ who may use it ============================
@pytest.mark.parametrize("who", ["board", "manager", "committee", "dual_other"])
def test_committee_board_and_manager_can_add_and_read_observations(client, w, who):
    r = post(client, getattr(w, who), w.v)
    assert r.status_code == 302 and SiteVisitObservation.objects.filter(violation=w.v, comment=SECRET).count() == 1
    page = client.get(f"/committee/meetings/{w.m.pk}/").content.decode()
    assert "Site-visit observations" in page and SECRET in page


@pytest.mark.parametrize("who", ["home_a", "home_b"])
def test_homeowners_can_never_add_read_or_open_anything(client, w, who):
    obs = sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())])
    p = obs.photos.get()
    client.force_login(getattr(w, who))
    assert client.post(add_url(w.v), {"comment": "sneaky"}).status_code == 403
    assert client.get(f"/site-visit-photos/{p.pk}/").status_code == 403
    assert SiteVisitObservation.objects.count() == 1


def test_signed_out_visitors_are_sent_to_sign_in(client, w):
    p = sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())]).photos.get()
    assert client.post(add_url(w.v), {"comment": "x"}).status_code == 302 and "/accounts/login" in client.post(add_url(w.v), {"comment": "x"})["Location"]
    assert client.get(f"/site-visit-photos/{p.pk}/").status_code == 302
    assert SiteVisitObservation.objects.count() == 1


def test_owners_never_see_observations_on_any_page_pdf_or_json(client, w):
    obs = sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())])
    client.force_login(w.home_a)
    blobs = [client.get(u).content.decode() for u in ("/me/", "/me/violations/", f"/me/{w.v.pk}/", f"/me/hearings/{w.m.pk}/")]
    blobs.append(client.get(f"/me/{w.v.pk}/state/").content.decode())
    client.logout()
    blobs.append(client.get(f"/r/{w.v.token}/").content.decode())
    assert all(SECRET not in b and "Site-visit" not in b and "site-visit" not in b.lower() for b in blobs)
    n = Notice.objects.create(violation=w.v, kind="hearing")
    pdf = render_notice_pdf(n, "owner")
    assert SECRET.encode() not in pdf and b"Site-visit" not in pdf


def test_the_owners_live_page_signature_does_not_change_when_an_observation_is_added(client, w):
    client.force_login(w.home_a)
    before = client.get(f"/me/{w.v.pk}/state/").json()
    sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())])
    assert client.get(f"/me/{w.v.pk}/state/").json() == before


@pytest.mark.parametrize("who", ["dual_own", "board_owner"])
def test_nobody_can_see_or_add_observations_on_their_own_violation(client, w, who):
    """A committee or Board member who is the OWNER of the case is treated like any owner for this feature."""
    obs = sitevisits.add(w.manager, w.v, SECRET, [upload("a.jpg", jpeg())])
    u = getattr(w, who)
    client.force_login(u)
    assert client.post(add_url(w.v), {"comment": "mine"}).status_code == 403
    assert client.get(f"/site-visit-photos/{obs.photos.get().pk}/").status_code == 403
    assert SECRET not in client.get(f"/violations/{w.v.pk}/" if who == "board_owner" else f"/committee/meetings/{w.m.pk}/").content.decode()
    assert SiteVisitAccess.objects.filter(violation=w.v, user=u, action=A.DENIED).count() >= 2
    assert SiteVisitObservation.objects.count() == 1


def test_a_committee_member_listed_for_the_owners_household_is_also_turned_away(client, w):
    """The roster says this member belongs to the case owner's household even though their login is not linked."""
    stray = make_user("stray", "committee")
    CommitteeMember.objects.create(name="Stray Member", year=timezone.localdate().year, user=stray, owner=w.oa)
    client.force_login(stray)
    assert client.post(add_url(w.v), {"comment": "x"}).status_code == 403


def test_a_committee_member_cannot_reach_cases_that_are_not_on_their_agenda(client, w):
    post_ok = post(client, w.committee, w.far)
    assert post_ok.status_code == 403 and SiteVisitObservation.objects.count() == 0
    assert post(client, w.board, w.far).status_code == 302          # the Board and manager can, on any issued case


def test_draft_cases_have_no_observations(client, w):
    d = Violation.objects.create(owner=w.ob, vtype=w.v.vtype, description="d", cure_required="x", status=S.DRAFT)
    assert post(client, w.board, d).status_code == 403


# ============================ what is recorded ============================
def test_each_observation_records_who_when_which_case_and_meeting(client, w):
    before = timezone.now()
    post(client, w.committee, w.v)
    o = SiteVisitObservation.objects.get()
    assert (o.violation, o.meeting, o.author, o.author_name, o.author_role, o.comment) == (w.v, w.m, w.committee, "Cam Member", "committee", SECRET)
    assert before <= o.created_at <= timezone.now() and o.corrects is None
    post(client, w.manager, w.v, comment="Later, same visit")
    assert SiteVisitObservation.objects.get(comment="Later, same visit").author_role == "manager"
    assert [x.author_name for x in SiteVisitObservation.objects.order_by("created_at", "pk")] == ["Cam Member", "Ma Nager"]


def test_the_author_stays_on_the_record_if_the_login_is_removed(w):
    o = sitevisits.add(w.committee, w.v, SECRET, [])
    w.committee.delete()
    o.refresh_from_db()
    assert o.author is None and o.author_name == "Cam Member"


def test_the_original_photograph_is_kept_exactly_with_its_fingerprint_and_date(client, w):
    taken = datetime(2026, 11, 20, 8, 15, 30)
    data = jpeg("red", taken=taken)
    post(client, w.committee, w.v, photos=[upload("IMG_0042.JPG", data)])
    p = SiteVisitPhoto.objects.get()
    assert p.read_bytes() == data and p.sha256 == hashlib.sha256(data).hexdigest() and p.size == len(data)
    assert p.original_name == "IMG_0042.JPG" and p.taken_at == timezone.make_aware(taken) and p.intact()
    assert p.file.name.startswith("site-visits/") and p.file.name.endswith(".jpg") and "IMG_0042" not in p.file.name
    assert p.observation.violation == w.v and p.created_at <= timezone.now()


def test_a_photo_is_served_as_the_original_picture_and_never_cached(client, w):
    data = jpeg("blue")
    p = sitevisits.add(w.board, w.v, "", [upload("x.jpg", data)]).photos.get()
    client.force_login(w.committee)
    r = client.get(f"/site-visit-photos/{p.pk}/")
    assert r.status_code == 200 and b"".join(r.streaming_content) == data and r["Content-Type"] == "image/jpeg"
    assert "no-store" in r["Cache-Control"] and "sandbox" in r["Content-Security-Policy"] and r["X-Content-Type-Options"] == "nosniff"


def test_only_real_pictures_are_accepted_whatever_they_are_called(client, w):
    sneaky = io.BytesIO()
    Image.new("P", (4, 4)).save(sneaky, "GIF")
    client.force_login(w.board)
    r = client.post(add_url(w.v), {"comment": "", "photos": [upload("evil.html", sneaky.getvalue() + b"<script>x</script>", "text/html"),
                                                           upload("notes.txt", b"hello", "text/plain")], "back": "meeting"}, follow=True)
    stored = SiteVisitPhoto.objects.get()
    assert stored.file.name.endswith(".gif") and ".html" not in stored.file.name
    assert "notes.txt" in r.content.decode() and "not a JPG, PNG, GIF or WebP" in r.content.decode()


def test_limits_on_how_many_and_how_big(client, w, monkeypatch):
    post(client, w.board, w.v, photos=[upload(f"{i}.jpg", jpeg(size=(10 + i, 10))) for i in range(12)])
    assert SiteVisitPhoto.objects.count() == 10
    monkeypatch.setattr(uploads, "MAX_UPLOAD", 10)
    post(client, w.board, w.v, comment="again", photos=[upload("big.jpg", jpeg())])
    assert SiteVisitPhoto.objects.count() == 10 and SiteVisitObservation.objects.filter(comment="again").exists()


def test_an_empty_submission_and_an_oversized_note_are_refused(client, w):
    post(client, w.board, w.v, comment="   ")
    post(client, w.board, w.v, comment="x" * (sitevisits.MAX_COMMENT + 1))
    assert SiteVisitObservation.objects.count() == 0
    assert post(client, w.board, w.v, comment="x" * sitevisits.MAX_COMMENT).status_code == 302 and SiteVisitObservation.objects.count() == 1


def test_files_use_private_storage_with_no_public_address_and_no_disk_path(client, w):
    """Same storage as photos and saved letters (so a private bucket in production); read through the storage, never a file path."""
    with mock.patch("django.db.models.fields.files.FieldFile.path", new_callable=mock.PropertyMock, side_effect=NotImplementedError):
        obs = sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())])
        p = obs.photos.get()
        client.force_login(w.board)
        assert client.get(f"/site-visit-photos/{p.pk}/").status_code == 200
        assert zipfile.ZipFile(io.BytesIO(body(client.get(f"/violations/{w.v.pk}/case-file.zip"))))
    assert client.get(f"/media/{p.file.name}").status_code == 404          # there is no public address for any stored file


# ============================ add-only ============================
def test_observations_photos_and_the_access_log_can_never_be_changed_or_deleted(w):
    o = sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())])
    p, a = o.photos.get(), SiteVisitAccess.objects.filter(observation=o).first()
    for rec, field in ((o, "comment"), (p, "sha256"), (a, "detail")):
        setattr(rec, field, "changed")
        with pytest.raises(ValidationError):
            rec.save()
        with pytest.raises(PermissionDenied):
            rec.delete()
    o.refresh_from_db()
    assert o.comment == SECRET and SiteVisitPhoto.objects.get().sha256 == hashlib.sha256(p.read_bytes()).hexdigest()


def test_the_admin_shows_them_read_only(w):
    from django.contrib import admin as dj_admin
    o = sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())])
    for model in (SiteVisitObservation, SiteVisitPhoto, SiteVisitAccess):
        ma = dj_admin.site._registry[model]
        assert not (ma.has_add_permission(None) or ma.has_change_permission(None, o) or ma.has_delete_permission(None, o))


def test_there_is_no_page_that_edits_or_deletes_and_posting_an_id_cannot_overwrite(client, w):
    from django.urls import get_resolver
    names = {p.name for p in get_resolver().url_patterns if getattr(p, "name", None)}
    assert not {n for n in names if "site_visit" in n} - {"site_visit_add", "site_visit_photo"}
    o = sitevisits.add(w.board, w.v, SECRET, [])
    client.force_login(w.board)
    for url in (f"/committee/cases/{w.v.pk}/observations/{o.pk}/", f"/committee/cases/{w.v.pk}/observations/{o.pk}/delete/", f"/site-visit-photos/{o.pk}/delete/"):
        assert client.post(url, {"comment": "x"}).status_code == 404
    client.post(add_url(w.v), {"id": o.pk, "pk": o.pk, "comment": "new one", "back": "case"})
    o.refresh_from_db()
    assert o.comment == SECRET and SiteVisitObservation.objects.count() == 2


def test_a_correction_is_a_new_observation_that_refers_to_the_original(client, w):
    o = sitevisits.add(w.committee, w.v, SECRET, [upload("a.jpg", jpeg())])
    post(client, w.board, w.v, comment="Correction: it was the neighbour's can, not theirs", corrects=o.pk)
    c = SiteVisitObservation.objects.get(corrects=o)
    assert c.pk != o.pk and c.author == w.board and list(o.corrections.all()) == [c]
    o.refresh_from_db()
    assert o.comment == SECRET and o.photos.count() == 1
    page = client.get(f"/violations/{w.v.pk}/").content.decode()
    assert f"Correction to #{o.pk}" in page and SECRET in page and "neighbour" in page


def test_a_correction_needs_words_and_must_belong_to_the_same_case(client, w):
    o = sitevisits.add(w.board, w.v, SECRET, [])
    other = sitevisits.add(w.board, w.far, "elsewhere", [])
    post(client, w.board, w.v, comment="", corrects=o.pk, photos=[upload("a.jpg", jpeg())])
    post(client, w.board, w.v, comment="wrong case", corrects=other.pk)
    post(client, w.board, w.v, comment="junk id", corrects="abc")
    assert SiteVisitObservation.objects.filter(corrects__isnull=False).count() == 0


# ============================ separate from the committee's findings ============================
def test_an_observation_never_becomes_a_committee_finding_or_changes_the_case(client, w):
    before = Violation.objects.values().get(pk=w.v.pk)
    post(client, w.committee, w.v, photos=[upload("a.jpg", jpeg())])
    assert Violation.objects.values().get(pk=w.v.pk) == before                      # no field of the case changed (findings, outcome, votes, status...)
    assert SECRET not in "\n".join(" ".join(map(str, s)) for s in minutes.build(w.m)) and SECRET not in minutes.render_pdf(w.m).decode("latin-1")
    w.v.hearing_date = timezone.now() - timedelta(hours=1)
    w.v.save()
    with pytest.raises(services.WorkflowError):                                      # the decision still needs the committee's own findings
        services.committee_record(w.v, "confirm", w.committee, votes_for=3, votes_against=0, findings="")


def test_the_decision_letter_and_findings_text_do_not_include_observations(w):
    sitevisits.add(w.board, w.v, SECRET, [])
    w.v.committee_findings, w.v.committee_votes_for, w.v.committee_votes_against, w.v.fine_amount = "The committee's own findings.", 3, 0, 100
    w.v.save()
    n = Notice.objects.create(violation=w.v, kind="fine")
    assert SECRET.encode() not in render_notice_pdf(n, "owner")


# ============================ integrity ============================
def test_a_tampered_or_missing_photo_is_caught(client, w):
    p = sitevisits.add(w.board, w.v, "", [upload("a.jpg", jpeg())]).photos.get()
    client.force_login(w.board)
    assert client.get(f"/site-visit-photos/{p.pk}/").status_code == 200
    with open(p.file.path, "ab") as f:
        f.write(b"tampered")
    assert not p.intact() and client.get(f"/site-visit-photos/{p.pk}/").status_code == 409
    import os
    os.remove(p.file.path)
    assert client.get(f"/site-visit-photos/{p.pk}/").status_code == 404


# ============================ the case-file zip ============================
def download(client, user, v):
    client.force_login(user)
    r = client.get(f"/violations/{v.pk}/case-file.zip")
    assert r.status_code == 200
    return zipfile.ZipFile(io.BytesIO(body(r)))


def rows(z, suffix):
    return list(csv.DictReader(io.StringIO(next(z.read(n) for n in z.namelist() if n.endswith(suffix)).decode("utf-8-sig"))))


def test_the_zip_has_a_site_visit_folder_with_notes_photos_manifest_and_access_history(client, w):
    d1, d2 = jpeg("red", taken=datetime(2026, 11, 20, 8, 15, 30)), jpeg("blue")
    o = sitevisits.add(w.committee, w.v, SECRET, [upload("one.jpg", d1), upload("two.jpg", d2)])
    c = sitevisits.add(w.board, w.v, "Correction note", [], corrects_id=o.pk)
    z = download(client, w.board, w.v)
    root = z.namelist()[0].split("/")[0]
    sv = [n for n in z.namelist() if f"/Site-Visit-Observations/" in n]
    assert f"{root}/Site-Visit-Observations/observations.csv" in sv and f"{root}/Site-Visit-Observations/photos-manifest.csv" in sv
    assert f"{root}/Site-Visit-Observations/access-history.csv" in sv and len([n for n in sv if "/photos/" in n]) == 2
    obs = {r["Observation"]: r for r in rows(z, "Site-Visit-Observations/observations.csv")}
    assert obs[str(o.pk)]["Comment"] == SECRET and obs[str(o.pk)]["By"] == "Cam Member" and obs[str(o.pk)]["Role"] == "committee"
    assert obs[str(o.pk)]["Submitted"] == timezone.localtime(o.created_at).strftime("%Y-%m-%d %H:%M") and obs[str(o.pk)]["Photos"] == "2"
    assert obs[str(c.pk)]["Corrects observation"] == str(o.pk)
    manifest = rows(z, "photos-manifest.csv")
    assert {r["SHA-256 fingerprint"] for r in manifest} == {hashlib.sha256(d1).hexdigest(), hashlib.sha256(d2).hexdigest()}
    for r in manifest:
        data = z.read(next(n for n in z.namelist() if n.endswith(r["File"].split("/", 1)[1])))
        assert hashlib.sha256(data).hexdigest() == r["SHA-256 fingerprint"] and r["File matches fingerprint"] == "yes" and data in (d1, d2)
    assert {r["Original file name"] for r in manifest} == {"one.jpg", "two.jpg"}
    assert any(r["What"] == "Added an observation" and r["Who"] == "Cam Member" for r in rows(z, "access-history.csv"))


def test_the_index_and_the_manifest_list_the_site_visit_files(client, w):
    sitevisits.add(w.committee, w.v, SECRET, [upload("one.jpg", jpeg("red"))])
    z = download(client, w.board, w.v)
    index = next(z.read(n) for n in z.namelist() if n.endswith("00-INDEX.html")).decode()
    assert "Site-Visit Observations (internal)" in index and "Site-Visit-Observations/observations.csv" in index and "photographs with fingerprints" in index
    assert "Photo from observation" in index and "manifest.csv" in index
    manifest = {r["File"]: r for r in rows(z, "/manifest.csv")}
    sv_files = [f for f in manifest if f.startswith("Site-Visit-Observations/")]
    assert len(sv_files) == 4                                                        # observations, photos manifest, access history, one photo
    for f, r in manifest.items():
        data = z.read(next(n for n in z.namelist() if n.endswith("/" + f)))
        assert hashlib.sha256(data).hexdigest() == r["SHA-256 fingerprint"] and int(r["Bytes"]) == len(data)


def test_the_zip_notes_a_damaged_photo_instead_of_failing(client, w):
    p = sitevisits.add(w.board, w.v, "", [upload("a.jpg", jpeg())]).photos.get()
    with open(p.file.path, "ab") as f:
        f.write(b"tampered")
    z = download(client, w.board, w.v)
    assert "DOES NOT MATCH" in next(z.read(n) for n in z.namelist() if n.endswith("00-INDEX.html")).decode()


def test_a_board_member_who_owns_the_violation_gets_a_zip_without_observations(client, w):
    sitevisits.add(w.manager, w.v, SECRET, [upload("a.jpg", jpeg())])
    z = download(client, w.board_owner, w.v)
    blob = b" ".join(z.read(n) for n in z.namelist() if not n.endswith((".pdf", ".jpg")))
    assert not [n for n in z.namelist() if "Site-Visit-Observations" in n] and SECRET.encode() not in blob
    assert "Not included in this download" in next(z.read(n) for n in z.namelist() if n.endswith("00-INDEX.html")).decode()
    assert SiteVisitAccess.objects.filter(action=A.EXPORTED, user=w.board_owner, detail__contains="NOT included").exists()


def test_only_the_board_and_manager_can_download_the_zip_even_for_a_committee_member(client, w):
    client.force_login(w.committee)
    assert client.get(f"/violations/{w.v.pk}/case-file.zip").status_code == 403
    client.force_login(w.home_b)
    assert client.get(f"/violations/{w.v.pk}/case-file.zip").status_code == 403


# ============================ audit history ============================
def test_adding_viewing_opening_and_exporting_are_all_recorded(client, w):
    client.force_login(w.committee)
    client.post(add_url(w.v), {"comment": SECRET, "photos": [upload("a.jpg", jpeg())], "back": "meeting"})
    o = SiteVisitObservation.objects.get()
    client.get(f"/committee/meetings/{w.m.pk}/")
    client.get(f"/site-visit-photos/{o.photos.get().pk}/")
    download(client, w.board, w.v)
    log = list(SiteVisitAccess.objects.filter(violation=w.v).order_by("pk"))
    assert [a.action for a in log] == [A.ADDED, A.VIEWED, A.PHOTO, A.EXPORTED]
    assert all(a.at and a.user_name and a.role and a.violation == w.v for a in log)
    assert (log[0].user, log[0].role, log[0].observation) == (w.committee, "committee", o) and log[3].user == w.board and "included" in log[3].detail
    assert Event.objects.filter(violation=w.v, text__startswith=f"Site-visit observation #{o.pk} added by Cam Member (1 photo)").exists()
    assert Event.objects.filter(violation=w.v, text="Case file downloaded", user=w.board).exists()
    assert not Event.objects.filter(violation=w.v, text__contains=SECRET).exists()          # the audit history never holds the note itself


def test_repeat_views_and_thumbnail_loads_are_not_logged_again_within_ten_minutes(client, w):
    p = sitevisits.add(w.board, w.v, SECRET, [upload("a.jpg", jpeg())]).photos.get()
    client.force_login(w.committee)
    for _ in range(3):
        client.get(f"/committee/meetings/{w.m.pk}/")
        client.get(f"/site-visit-photos/{p.pk}/")
    assert SiteVisitAccess.objects.filter(user=w.committee, action=A.VIEWED).count() == 1
    assert SiteVisitAccess.objects.filter(user=w.committee, action=A.PHOTO).count() == 1
    client.force_login(w.manager)
    client.get(f"/violations/{w.v.pk}/")
    assert SiteVisitAccess.objects.filter(user=w.manager, action=A.VIEWED).count() == 1          # another person is logged separately


def test_the_log_records_where_the_request_came_from_and_refused_attempts(client, w):
    client.force_login(w.dual_own)
    client.post(add_url(w.v), {"comment": "x"}, REMOTE_ADDR="203.0.113.9")
    denied = SiteVisitAccess.objects.get(action=A.DENIED)
    assert denied.user == w.dual_own and denied.ip_address == "203.0.113.9" and "own" in denied.detail


# ============================ the pages ============================
def test_each_case_on_the_meeting_page_has_the_section_with_the_add_form(client, w):
    client.force_login(w.committee)
    page = client.get(f"/committee/meetings/{w.m.pk}/").content.decode()
    assert f'id="site-visits-{w.v.pk}"' in page and "Site-visit observations" in page and "They are <b>not</b> the committee's findings" in page
    assert f'action="/committee/cases/{w.v.pk}/observations/"' in page and 'name="photos"' in page and "Save observation" in page


def test_the_meeting_page_tells_a_conflicted_member_they_cannot_see_their_own_cases_observations(client, w):
    sitevisits.add(w.board, w.v, SECRET, [])
    client.force_login(w.dual_own)
    page = client.get(f"/committee/meetings/{w.m.pk}/").content.decode()
    import html
    assert SECRET not in page and "your own (or your household's) violation" in html.unescape(page)


def test_the_staff_case_page_shows_the_section_and_adding_there_returns_to_it(client, w):
    client.force_login(w.manager)
    r = client.post(add_url(w.v), {"comment": SECRET, "back": "case"})
    assert r.status_code == 302 and r["Location"].endswith(f"/violations/{w.v.pk}/#site-visits")
    assert SECRET in client.get(f"/violations/{w.v.pk}/").content.decode()
    client.force_login(w.committee)
    assert client.post(add_url(w.v), {"comment": "again", "back": "case"})["Location"].endswith(f"/committee/meetings/{w.m.pk}/#case-{w.v.pk}")   # no way to a staff page


def test_notes_are_shown_escaped(client, w):
    sitevisits.add(w.board, w.v, "<script>alert(1)</script> & more", [])
    client.force_login(w.board)
    page = client.get(f"/violations/{w.v.pk}/").content.decode()
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in page


def test_nothing_about_suspension_or_the_letters_changed(client, settings, w):
    settings.SUSPENSION_ENABLED = False
    sitevisits.add(w.board, w.v, SECRET, [])
    client.force_login(w.board)
    assert "uspension" not in client.get(f"/violations/{w.v.pk}/").content.decode()
