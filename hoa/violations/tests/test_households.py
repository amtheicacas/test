import io
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from violations import accounts, geocoding, services, vantaca
from violations.models import Owner, Photo, Profile, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()
PW = "a-long-test-password-123"


@pytest.fixture(autouse=True)
def _m(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def png():
    b = io.BytesIO()
    Image.new("RGB", (30, 20)).save(b, "PNG")
    return SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png")


def household(**kw):
    o = Owner.objects.create(name="Pat Owner", property_address=f"{Owner.objects.count() + 1} Palm Way", email="pat@example.com",
                             co_owner_email=kw.pop("co", "sam@example.com"), zip_code="34241", **kw)
    t = ViolationType.objects.get_or_create(name="Shed")[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.NOTICE_SENT,
                                 cure_deadline=date.today() + timedelta(days=5))
    Photo.objects.create(violation=v, image=png())
    return o, v


def login(client, user):
    client.force_login(user)


# ---------- several logins, one household ----------
def test_two_logins_see_the_same_violations_and_each_others_messages(client):
    o, v = household()
    pat = accounts.invite_member(o, "pat@example.com", "Pat Owner", "owner").user
    sam = accounts.invite_member(o, "sam@example.com", "Sam Owner", "co_owner").user
    login(client, pat)
    client.post(f"/me/{v.pk}/", {"message": "Pat here: contractor booked for Friday."})
    client.logout()
    login(client, sam)
    page = client.get(f"/me/{v.pk}/").content.decode()
    assert "contractor booked for Friday" in page and ">Pat<" in page  # Sam sees Pat's message, labelled with Pat's name
    client.post(f"/me/{v.pk}/", {"message": "Sam: confirming."})
    client.logout()
    login(client, pat)
    page = client.get(f"/me/{v.pk}/").content.decode()
    assert "Sam: confirming." in page and ">Sam<" in page and ">You<" in page
    assert client.get("/me/").content.decode().count(f"/me/{v.pk}/") >= 1


def test_a_second_household_cannot_see_the_first(client):
    o1, v1 = household()
    o2, v2 = household(co="")
    other = accounts.invite_member(o2, "other@example.com", "Other", "owner").user
    login(client, other)
    assert client.get(f"/me/{v1.pk}/").status_code == 404 and client.get(f"/me/{v2.pk}/").status_code == 200


def test_household_page_lets_you_add_and_remove_people(client):
    o, v = household(co="")
    pat = accounts.invite_member(o, "pat@example.com", "Pat Owner", "owner").user
    login(client, pat)
    assert b"My household" in client.get("/me/household/").content
    mail.outbox.clear()
    client.post("/me/household/", {"name": "Sam Owner", "email": "sam@example.com", "relationship": "co_owner"})
    sam = U.objects.get(username="sam@example.com")
    assert sam.profile.owner == o and sam.profile.relationship == "co_owner" and sam.first_name == "Sam"
    assert len(mail.outbox) == 1 and "/accounts/reset/" in mail.outbox[0].body
    client.post("/me/household/", {"remove": sam.profile.pk})
    sam.refresh_from_db()
    assert sam.is_active is False
    client.post("/me/household/", {"remove": pat.profile.pk})  # can't remove yourself
    pat.refresh_from_db()
    assert pat.is_active is True


def test_household_login_limit_and_duplicates(client, settings):
    settings.MAX_LOGINS_PER_HOUSEHOLD = 2
    o, _ = household(co="")
    assert accounts.invite_member(o, "a@example.com", "A") and accounts.invite_member(o, "b@example.com", "B")
    assert accounts.invite_member(o, "c@example.com", "C") is None  # full
    assert accounts.invite_member(o, "a@example.com", "A again") is None  # already has a login
    login(client, U.objects.get(username="a@example.com"))
    assert b"limit of logins" in client.get("/me/household/").content


def test_only_homeowners_have_a_household_page(client):
    u = U.objects.create_user("mgr", password=PW)
    Profile.objects.create(user=u, role="manager")
    login(client, u)
    assert client.get("/me/household/").status_code == 403


# ---------- everyone on the deed / lease can get access themselves ----------
@pytest.mark.parametrize("who,expected", [("pat@example.com", "owner"), ("sam@example.com", "co_owner"), ("tina@example.com", "tenant")])
def test_get_access_works_for_owner_coowner_and_tenant_emails(client, who, expected):
    o, _ = household(tenant_name="Tina Tenant", tenant_email="tina@example.com")
    client.post("/accounts/get-access/", {"email": who})
    p = Profile.objects.get(user__email=who)
    assert p.owner == o and p.relationship == expected and len(mail.outbox) == 1
    client.post("/accounts/get-access/", {"email": who})  # again: a password-reset link, still one login
    assert Profile.objects.filter(user__email=who).count() == 1 and len(mail.outbox) == 2


def test_tenant_login_sees_the_same_case_as_the_owner(client):
    o, v = household(tenant_name="Tina Tenant", tenant_email="tina@example.com")
    tina = accounts.invite_member(o, "tina@example.com", "Tina Tenant", "tenant").user
    login(client, tina)
    assert client.get(f"/me/{v.pk}/").status_code == 200


def test_owner_leaving_switches_off_every_login_in_the_household():
    o, _ = household(tenant_name="Tina", tenant_email="tina@example.com")
    users = [accounts.invite_member(o, e, e, r).user for e, r in
             [("pat@example.com", "owner"), ("sam@example.com", "co_owner"), ("tina@example.com", "tenant")]]
    o.active = False
    o.save()
    assert accounts.sync_account(o) == "deactivated"
    assert all(not U.objects.get(pk=u.pk).is_active for u in users)


# ---------- notices to owner, co-owner and tenant ----------
def test_notice_goes_to_owner_coowner_and_tenant_as_separate_emails():
    o, v = household(tenant_name="Tina Tenant", tenant_email="tina@example.com")
    mail.outbox.clear()
    services.send_notice(v, "violation")
    to = {tuple(m.to): m.body.split("\n")[0] for m in mail.outbox}
    assert to == {("pat@example.com", "sam@example.com"): "Dear Pat Owner,", ("tina@example.com",): "Dear Tina Tenant,"}
    assert all(m.attachments[0][2] == "application/pdf" for m in mail.outbox)


# ---------- Vantaca feed: co-owner and tenant columns ----------
CSV = ("Account ID,Owner Name,Property Address,ZIP,Email,Co-owner Email,Tenant Name,Tenant Email\n"
       "A1,Pat Owner,12 Palm Way #3,34241,pat@example.com,sam@example.com,Tina Tenant,tina@example.com\n")


def test_feed_carries_coowner_and_tenant_and_a_blank_means_moved_out():
    vantaca.sync_csv(CSV)
    o = Owner.objects.get()
    assert (o.co_owner_email, o.tenant_name, o.tenant_email) == ("sam@example.com", "Tina Tenant", "tina@example.com")
    vantaca.sync_csv(CSV.replace("Tina Tenant,tina@example.com", ","))
    o.refresh_from_db()
    assert o.tenant_name == "" and o.co_owner_email == "sam@example.com"


def test_feed_without_tenant_columns_leaves_tenants_alone():
    vantaca.sync_csv(CSV)
    vantaca.sync_csv("Account ID,Owner Name,Property Address,ZIP,Email\nA1,Pat Owner,12 Palm Way #3,34241,pat@example.com\n")
    assert Owner.objects.get().tenant_name == "Tina Tenant"


# ---------- import page ----------
def manager(client):
    u = U.objects.create_user("mgr", password=PW)
    Profile.objects.create(user=u, role="manager")
    client.force_login(u)


def upload(client, text, **extra):
    return client.post("/import/", {"file": SimpleUploadedFile("o.csv", text.encode(), content_type="text/csv"), **extra})


def test_import_preview_changes_nothing_then_import_applies(client):
    manager(client)
    text = CSV + "A2,,5 Oak Ln,,bad-email,,,\nA3,Lee,7 Bay St,,lee@x,,,\n"
    r = upload(client, text)
    body = r.content.decode()
    assert "Preview of 3 rows" in body and Owner.objects.count() == 0
    assert "needs an owner name" in body and "doesn&#x27;t look like a valid email" in body and "No ZIP code" in body
    assert upload(client, text, apply="1").status_code == 302
    assert Owner.objects.count() == 2  # the row with no name is skipped


def test_import_is_manager_only_and_template_downloads(client):
    u = U.objects.create_user("board", password=PW)
    Profile.objects.create(user=u, role="board")
    client.force_login(u)
    assert client.get("/import/").status_code == 403 and client.get("/import/template.csv").status_code == 403
    manager(client)
    t = client.get("/import/template.csv").content.decode()
    assert t.startswith("Account ID,Owner Name,Property Address") and "Tenant Email" in t
    rows, result, problems = vantaca.preview_csv(t)  # the template itself imports cleanly
    assert result["created"] == 1 and problems == []


def test_check_rows_flags_duplicates():
    _, _, problems = vantaca.preview_csv("Owner Name,Property Address,ZIP\nA,1 X St,34241\nB,1 x st,34241\n")
    assert any("Same property address as row 1" in m for _, m in problems)


# ---------- map ----------
CENSUS = ('"1","12 Heron Way, Sarasota, FL, 34241","Match","Exact","12 HERON WAY, SARASOTA, FL, 34241","-82.4,27.3","123","L"\n'
          '"2","99 Nowhere, Sarasota, FL, 34241","No_Match"\n')


def test_census_batch_reply_is_parsed():
    assert geocoding.parse(CENSUS) == {"1": (27.3, -82.4)}


def test_geocode_missing_sends_street_without_unit_and_saves_matches():
    a = Owner.objects.create(name="A", property_address="12 Heron Way #52", zip_code="34241")
    Owner.objects.create(name="B", property_address="99 Nowhere", zip_code="34241")
    sent = []
    reply = CENSUS.replace('"1"', f'"{a.pk}"').replace('"2"', f'"{a.pk + 1}"')
    located, missing = geocoding.geocode_missing(poster=lambda rows: sent.extend(rows) or reply)
    a.refresh_from_db()
    assert (located, missing) == (1, 1) and (a.lat, a.lng) == (27.3, -82.4)
    assert sent[0][1] == "12 Heron Way"  # "#52" removed so the building matches


def test_units_in_one_building_get_separate_dots_and_search_data(client):
    manager(client)
    for i in (1, 2, 3):
        Owner.objects.create(name=f"O{i}", property_address=f"7335 Great Egret #{i}", lat=27.3, lng=-82.5, tenant_name="T" if i == 2 else "")
    homes = client.get("/api/map/").json()["homes"]
    assert len({(round(h["lat"], 6), round(h["lng"], 6)) for h in homes}) == 3
    assert max(abs(h["lat"] - 27.3) for h in homes) < 0.0002  # still on the building
    assert [h["rented"] for h in sorted(homes, key=lambda h: h["address"])] == [False, True, False]
    page = client.get("/map/").content.decode()
    assert "msearch" in page


def test_locate_button_is_manager_only_and_uses_the_geocoder(client, monkeypatch):
    Owner.objects.create(name="A", property_address="12 Heron Way", zip_code="34241")
    monkeypatch.setattr(geocoding, "_post", lambda rows: f'"{rows[0][0]}","x","Match","Exact","x","-82.4,27.3","1","L"\n')
    u = U.objects.create_user("board", password=PW)
    Profile.objects.create(user=u, role="board")
    client.force_login(u)
    assert client.post("/map/locate/").status_code == 403
    manager(client)
    assert client.post("/map/locate/").status_code == 302 and Owner.objects.get().lat == 27.3


# ---------- board prefill ----------
def test_board_queue_prefills_the_types_usual_fine(client):
    manager(client)
    o, v = household()
    v.status, v.cure_deadline = S.PENDING_BOARD, date.today() - timedelta(days=3)
    v.save()
    v.vtype.default_fine = 75
    v.vtype.save()
    page = client.get("/board/").content.decode()
    assert 'value="75.00"' in page and "Board votes for" in page and "Suspension: number of days" in page


def test_locate_button_reports_a_failed_lookup_instead_of_crashing(client, monkeypatch):
    def boom(rows):
        raise OSError("blocked")
    monkeypatch.setattr(geocoding, "_post", boom)
    Owner.objects.create(name="A", property_address="12 Heron Way", zip_code="34241")
    manager(client)
    r = client.post("/map/locate/", follow=True)
    assert r.status_code == 200 and "Couldn&#x27;t reach the address lookup service" in r.content.decode()


# ---------- upload formats ----------
BASE = "Owner Name,Property Address,ZIP\nPat,12 Palm Way #3,34241\nSam,14 Palm Way,34241\n"


@pytest.mark.parametrize("name,data", [
    ("unix", BASE.encode()),
    ("windows", BASE.replace("\n", "\r\n").encode()),
    ("old-mac (CR only)", BASE.replace("\n", "\r").encode()),
    ("mixed", "Owner Name,Property Address,ZIP\r\nPat,12 Palm Way #3,34241\rSam,14 Palm Way,34241\n".encode()),
    ("bom", b"\xef\xbb\xbf" + BASE.encode()),
    ("semicolons", BASE.replace(",", ";").encode()),
    ("tabs", BASE.replace(",", "\t").encode()),
    ("windows-1252", BASE.replace("Pat", "José").encode("cp1252")),
    ("blank lines", ("\n" + BASE.replace("\n", "\n\n")).encode()),
])
def test_any_common_csv_layout_reads(name, data):
    rows = vantaca.rows_from_upload(data)
    assert len(rows) == 2 and rows[0]["Property Address"] == "12 Palm Way #3" and rows[1]["ZIP"] == "34241", name


def test_excel_workbooks_read_including_numeric_zip_codes():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Owner Name", "Property Address", "ZIP", "Email"])
    ws.append(["Pat", "12 Palm Way #3", 34241, "pat@example.com"])
    ws.append([None, None, None, None])
    ws.append(["Sam", "14 Palm Way", 34241.0, None])
    buf = io.BytesIO()
    wb.save(buf)
    rows = vantaca.rows_from_upload(buf.getvalue(), "owners.xlsx")
    assert rows == [{"Owner Name": "Pat", "Property Address": "12 Palm Way #3", "ZIP": "34241", "Email": "pat@example.com"},
                    {"Owner Name": "Sam", "Property Address": "14 Palm Way", "ZIP": "34241", "Email": ""}]


@pytest.mark.parametrize("data,msg", [
    (b"", "empty"), (b"Owner Name,Property Address\n", "header row"), (b"\xd0\xcf\x11\xe0" + b"x" * 50, "old-style Excel"),
    (b"PK\x03\x04 not really a workbook", "Excel file couldn't be read"), (b"a\x00b\x00c", "doesn't look like"),
])
def test_bad_files_get_a_clear_message(data, msg):
    with pytest.raises(vantaca.UploadError, match=msg):
        vantaca.rows_from_upload(data)


def test_import_page_shows_a_friendly_error_not_a_crash(client):
    manager(client)
    r = client.post("/import/", {"file": SimpleUploadedFile("x.xls", b"\xd0\xcf\x11\xe0" + b"x" * 50)})
    assert r.status_code == 200 and "old-style Excel" in r.content.decode() and Owner.objects.count() == 0


def test_import_page_accepts_a_mac_csv_and_an_xlsx(client):
    manager(client)
    mac = BASE.replace("\n", "\r").encode()
    assert "Preview of 2 rows" in client.post("/import/", {"file": SimpleUploadedFile("o.csv", mac)}).content.decode()
    assert client.post("/import/", {"file": SimpleUploadedFile("o.csv", mac), "apply": "1"}).status_code == 302
    assert Owner.objects.count() == 2
