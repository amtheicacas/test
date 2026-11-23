"""A3: the case-file zip (Board and manager only), and the log of every download."""
import csv
import hashlib
import io
import os
import zipfile
from datetime import date, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image

from violations import mailing, services
from violations.models import CommitteeMeeting, CommitteeMember, Event, MeetingAttendance, Notice, Owner, Photo, Profile, Response, Violation, ViolationType
from violations.tests.review4_support import body

S = Violation.Status
K = Notice.Kind
pytestmark = pytest.mark.django_db
U = get_user_model()


def staff(name, role="board"):
    u = U.objects.create_user(name, f"{name}@example.com", "x", first_name="Bo", last_name="Ard")
    Profile.objects.create(user=u, role=role)
    return u


def png(color="green"):
    b = io.BytesIO()
    Image.new("RGB", (30, 20), color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def full_case(settings):
    """A case with a notice emailed and mailed (certified tracking), photos, messages, a hearing and a committee decision."""
    settings.MAIL_PROVIDER, settings.LOB_API_KEY, settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "lob", "k", "PO Box 1", "34241"
    o = Owner.objects.create(name="Pat Owner", property_address="12 Palm Way", city="Sarasota", state="FL", zip_code="34240", email="pat@example.com")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="Shed is in disrepair", cure_required="Repair the shed", cure_days=15,
                                 status=S.NOTICE_SENT, cure_deadline=date.today() + timedelta(days=15))
    p1 = Photo.objects.create(violation=v, image=ContentFile(png("green"), name="a.png"), taken_at=timezone.now() - timedelta(days=2))
    p2 = Photo.objects.create(violation=v, image=ContentFile(png("red"), name="b.png"), from_resident=True)
    Response.objects.create(violation=v, message="I will fix it this weekend", from_resident=True, extension_requested=True, extension_days=7)
    boss = staff("boss")
    with mock.patch.object(mailing, "lob_send", return_value={"id": "ltr_1", "tracking_number": "9407100000000000000009", "tracking_events": [
            {"name": "In Transit", "location": "TAMPA FL", "time": "2026-11-20T10:00:00Z"}]}):
        nov = services.send_notice(v, K.VIOLATION, boss)
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30))
    member = CommitteeMember.objects.create(name="Casey Member", year=timezone.localdate().year)
    MeetingAttendance.objects.create(meeting=m, member=member, status="present")
    v.committee_meeting, v.hearing_date, v.hearing_location = m, m.starts_at, "Clubhouse"
    v.board_meeting_date, v.proposed_fine, v.board_votes_for, v.board_votes_against = date.today(), 250, 4, 1
    v.committee_outcome, v.committee_votes_for, v.committee_votes_against = "confirm", 3, 0
    v.committee_findings, v.fine_amount, v.fine_due_on = "The shed was not repaired by the deadline.", 250, date.today() + timedelta(days=35)
    v.save()
    return v, boss, nov, (p1, p2)


def download(client, user, v):
    client.force_login(user)
    r = client.get(f"/violations/{v.pk}/case-file.zip")
    assert r.status_code == 200 and r["Content-Type"] == "application/zip"
    return r, zipfile.ZipFile(io.BytesIO(body(r)))


def names(z):
    return [n.split("/", 1)[1] for n in z.namelist()]


def read(z, suffix):
    return next(z.read(n) for n in z.namelist() if n.endswith(suffix))


def test_the_zip_holds_every_part_of_the_case(client, full_case):
    v, boss, nov, (p1, p2) = full_case
    r, z = download(client, boss, v)
    assert r["Content-Disposition"] == f'attachment; filename="case-{v.pk}-12-Palm-Way.zip"'
    got = names(z)
    for expected in ("00-INDEX.html", "01-case-summary.txt", "letters/letters-manifest.csv", "emails/emails.csv", "mailing/delivery-summary.txt",
                     "mailing/pieces-and-tracking.csv", "mailing/tracking-events.csv", "photos/photos.csv",
                     "hearing-and-decision/hearing-and-decision.txt", "messages/messages-with-the-owner.csv", "history/case-history.csv"):
        assert expected in got, expected
    assert len([n for n in got if n.startswith("letters/") and n.endswith(".pdf")]) == nov.sent_copies.count() == 2      # emailed + certified/first-class
    assert len([n for n in got if n.startswith("emails/") and n.endswith(".txt")]) == 1
    assert len([n for n in got if n.startswith("photos/") and not n.endswith(".csv")]) == 2


def test_the_letters_in_the_zip_are_the_exact_saved_bytes_with_their_fingerprints(client, full_case):
    v, boss, nov, _ = full_case
    _, z = download(client, boss, v)
    manifest = list(csv.DictReader(io.StringIO(read(z, "letters-manifest.csv").decode("utf-8-sig"))))
    assert len(manifest) == 2
    for row in manifest:
        assert row["File matches fingerprint"] == "yes"
        data = z.read(next(n for n in z.namelist() if n.endswith(row["File"].split("/", 1)[1])))
        assert hashlib.sha256(data).hexdigest() == row["SHA-256 fingerprint"]
        assert any(c.read_bytes() == data for c in nov.sent_copies.all())
    assert any("9407100000000000000009" in row["Tracking number"] for row in manifest)


def test_photos_emails_mailing_hearing_messages_and_history_are_in_the_zip(client, full_case):
    v, boss, nov, (p1, p2) = full_case
    _, z = download(client, boss, v)
    photos = [n for n in z.namelist() if "/photos/photo-" in n]
    with open(p1.image.path, "rb") as a, open(p2.image.path, "rb") as b:
        assert sorted(z.read(n) for n in photos) == sorted([a.read(), b.read()])
    assert any("from-owner" in n for n in photos)
    emails = list(csv.DictReader(io.StringIO(read(z, "emails/emails.csv").decode("utf-8-sig"))))
    assert emails[0]["To"] == "pat@example.com" and emails[0]["Delivered"] == "yes"
    assert b"Notice of violation" in read(z, "delivery-summary.txt") and b"9407100000000000000009" in read(z, "pieces-and-tracking.csv")
    assert b"In Transit" in read(z, "tracking-events.csv")
    hearing = read(z, "hearing-and-decision.txt").decode()
    assert "Board vote: 4 for, 1 against" in hearing and "Decision: " in hearing and "3 for, 0 against" in hearing
    assert "The shed was not repaired by the deadline." in hearing and "Casey Member: present" in hearing and "Fine imposed: $250" in hearing
    assert "I will fix it this weekend" in read(z, "messages-with-the-owner.csv").decode("utf-8-sig")
    assert "Case file downloaded" in read(z, "case-history.csv").decode("utf-8-sig")


def test_the_index_lists_everything_with_dates_and_flags_letters_with_no_saved_copy(client, full_case):
    v, boss, nov, _ = full_case
    Notice.objects.create(violation=v, kind=K.EXTENSION)          # sent before copies were kept
    _, z = download(client, boss, v)
    html = read(z, "00-INDEX.html").decode()
    assert f"Case file: #{v.pk}" in html and "Pat Owner" in html and "12 Palm Way" in html
    for section in ("Letters (exact copies as sent)", "Emails", "Mailing", "Photos", "Hearing and decision", "Messages", "Change history"):
        assert section in html
    assert f"{timezone.localtime(nov.created_at):%Y-%m-%d}" in html and "letters/letters-manifest.csv" in html
    assert "Letters with no exact copy on file" in html and "Extension granted" in html and "fingerprint" in html


def test_every_download_is_logged_with_who_and_when(client, full_case):
    v, boss, _, _ = full_case
    other = staff("mgr", "manager")
    before = timezone.now()
    download(client, boss, v)
    download(client, other, v)
    logged = list(Event.objects.filter(violation=v, text="Case file downloaded").order_by("pk"))
    assert [e.user for e in logged] == [boss, other] and all(e.created_at >= before for e in logged)


def test_only_the_board_and_manager_can_download(client, full_case):
    v, boss, _, _ = full_case
    url = f"/violations/{v.pk}/case-file.zip"
    assert client.get(url).status_code == 302
    for name, role in (("cm", "committee"), ("ho", "homeowner")):
        client.force_login(staff(name, role))
        assert client.get(url).status_code == 403
    assert Event.objects.filter(text="Case file downloaded").count() == 0                    # refused attempts are not downloads
    for name, role in (("b2", "board"), ("m2", "manager")):
        client.force_login(staff(name, role))
        assert client.get(url).status_code == 200


def test_the_case_page_has_the_button_and_the_zip_holds_nothing_about_other_owners(client, full_case):
    v, boss, _, _ = full_case
    other_owner = Owner.objects.create(name="Zed Neighbor", property_address="77 Other Street", email="zed@example.com")
    other = Violation.objects.create(owner=other_owner, vtype=v.vtype, description="Other private matter", cure_required="x", status=S.NOTICE_SENT)
    Event.objects.create(violation=other, text="Secret note about Zed")
    client.force_login(boss)
    assert f"/violations/{v.pk}/case-file.zip" in client.get(f"/violations/{v.pk}/").content.decode()
    _, z = download(client, boss, v)
    blob = b" ".join(z.read(n) for n in z.namelist() if not n.endswith(".pdf") and not n.endswith(".png"))
    assert b"Zed" not in blob and b"77 Other Street" not in blob and b"Other private matter" not in blob


def test_a_photo_that_cannot_be_read_is_listed_not_fatal(client, full_case):
    v, boss, _, (p1, p2) = full_case
    os.remove(p2.image.path)
    _, z = download(client, boss, v)
    assert "(could not be read)" in read(z, "00-INDEX.html").decode() and len([n for n in z.namelist() if "/photos/photo-" in n]) == 1


def test_suspension_appears_in_the_case_file_only_when_it_is_switched_on(client, full_case, settings):
    v, boss, _, _ = full_case
    v.proposed_suspension, v.proposed_suspension_days, v.suspension, v.suspension_days = "the pool", 10, "the pool", 10
    v.save()
    settings.SUSPENSION_ENABLED = False
    _, z = download(client, boss, v)
    assert b"uspension" not in read(z, "hearing-and-decision.txt")
    settings.SUSPENSION_ENABLED = True
    _, z = download(client, boss, v)
    assert b"Proposed suspension: the pool" in read(z, "hearing-and-decision.txt")
