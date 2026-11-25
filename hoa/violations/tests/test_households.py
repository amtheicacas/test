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
    assert client.get("/me/violations/").content.decode().count(f"/me/{v.pk}/") >= 1


def test_a_second_household_cannot_see_the_first(client):
    o1, v1 = household()
    o2, v2 = household(co="")
    other = accounts.invite_member(o2, "other@example.com", "Other", "owner").user
    login(client, other)
    assert client.get(f"/me/{v1.pk}/").status_code == 404 and client.get(f"/me/{v2.pk}/").status_code == 200


def test_residents_cannot_change_their_information(client):
    """Owners and tenants can look, not edit: corrections go through the property manager and come from Vantaca."""
    o, v = household(tenant_name="Tina Tenant", tenant_email="tina@example.com")
    pat = accounts.invite_member(o, "pat@example.com", "Pat Owner", "owner").user
    login(client, pat)
    page = client.get("/me/household/").content.decode()
    assert "You can&#x27;t change it here" in page or "You can't change it here" in page
    assert "<form" not in page.split("Who has a login")[0].split("On file")[1]  # nothing to submit
    before = Owner.objects.values().get(pk=o.pk)
    for data in ({"name": "Hacked", "email": "evil@example.com", "relationship": "co_owner"}, {"remove": "1"}, {"property_address": "1 Evil St"}):
        assert client.post("/me/household/", data).status_code == 405  # read-only
    assert Owner.objects.values().get(pk=o.pk) == before and U.objects.filter(email="evil@example.com").count() == 0
    # and there is no other page that edits owner details
    for url in ("/me/profile/", "/me/edit/", "/me/account/", "/me/household/add/", "/me/household/remove/"):
        assert client.get(url).status_code == 404 and client.post(url, {"email": "x@example.com"}).status_code == 404


def test_the_household_page_shows_what_is_on_file_and_how_to_correct_it(client):
    o, _ = household(tenant_name="Tina Tenant", tenant_email="tina@example.com", mailing_address="PO Box 55, Tampa, FL 33601")
    pat = accounts.invite_member(o, "pat@example.com", "Pat Owner", "owner").user
    tina = accounts.invite_member(o, "tina@example.com", "Tina Tenant", "tenant").user
    login(client, pat)
    page = client.get("/me/household/").content.decode()
    assert "PO Box 55" in page and "pat@example.com" in page and "Tina Tenant" in page and "contact the property manager" in page
    client.logout()
    login(client, tina)  # a tenant sees their own details, not the owner's mailing address or email
    page = client.get("/me/household/").content.decode()
    assert "tina@example.com" in page and "PO Box 55" not in page and "Homeowner(s)" not in page


def test_household_login_limit_and_duplicates(settings):
    settings.MAX_LOGINS_PER_HOUSEHOLD = 2
    o, _ = household(co="")
    assert accounts.invite_member(o, "a@example.com", "A") and accounts.invite_member(o, "b@example.com", "B")
    assert accounts.invite_member(o, "c@example.com", "C") is None  # full
    assert accounts.invite_member(o, "a@example.com", "A again") is None  # already has a login


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


def upload(client, data, name="o.csv"):
    data = data.encode() if isinstance(data, str) else data
    return client.post("/import/", {"action": "upload", "file": SimpleUploadedFile(name, data)})


def walk(client, data, overrides=None, apply=True, invite=False):
    """Upload -> match columns (start from the guess, apply overrides {header: field}) -> preview -> import."""
    r = upload(client, data)
    cols = r.context["columns"]
    post = {f"map_{i}": (overrides or {}).get(h, cur) for i, h, _ex, cur in cols}
    pre = client.post("/import/", {"action": "map", **post})
    if not apply:
        return pre
    return client.post("/import/", {"action": "apply", **post, **({"invite": "on"} if invite else {})})


def test_import_is_staff_only_and_template_downloads(client):
    u = U.objects.create_user("cmte", password=PW)
    Profile.objects.create(user=u, role="committee")
    client.force_login(u)
    assert client.get("/import/").status_code == 403 and client.get("/import/template.csv").status_code == 403
    manager(client)
    t = client.get("/import/template.csv").content.decode()
    assert t.startswith("Homeowner ID,Account Number,Homeowner(s),Address,Mailing Address") and "Tenant Min End Date" in t
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


def test_locate_button_is_staff_only_and_uses_the_geocoder(client, monkeypatch):
    Owner.objects.create(name="A", property_address="12 Heron Way", zip_code="34241")
    monkeypatch.setattr(geocoding, "_post", lambda rows: f'"{rows[0][0]}","x","Match","Exact","x","-82.4,27.3","1","L"\n')
    u = U.objects.create_user("cmte", password=PW)
    Profile.objects.create(user=u, role="committee")
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
    assert 'value="75.00"' in page and "Suspension" not in page and "Board votes for" not in page


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




# ---------- column matching ----------
ODD = "Person,Where,Zone,Reach,Unit Notes\nPat Owner,12 Palm Way #3,34241,pat@example.com,gate code 1\n"


def test_upload_shows_the_matching_step_with_samples_and_saves_nothing(client):
    manager(client)
    r = upload(client, CSV)
    page = r.content.decode()
    assert "Step 2 of 3" in page and "Owner Name" in page and "pat@example.com" in page and Owner.objects.count() == 0
    guess = {h: cur for _i, h, _ex, cur in r.context["columns"]}
    assert guess["Owner Name"] == "name" and guess["Property Address"] == "property_address" and guess["Email"] == "email"
    assert guess["Co-owner Email"] == "co_owner_email" and guess["Tenant Name"] == "tenant_name"


def test_you_can_match_oddly_named_columns_and_unmatched_ones_are_ignored(client):
    manager(client)
    guess = {h: cur for _i, h, _ex, cur in upload(client, ODD).context["columns"]}
    assert set(guess.values()) == {""}  # nothing here looks like a known column name
    m = {"Person": "name", "Where": "property_address", "Zone": "zip_code", "Reach": "email"}
    pre = walk(client, ODD, m, apply=False)
    assert "Step 3 of 3" in pre.content.decode() and pre.context["preview"]["created"] == 1 and Owner.objects.count() == 0
    walk(client, ODD, m)
    o = Owner.objects.get()
    assert (o.name, o.property_address, o.zip_code, o.email) == ("Pat Owner", "12 Palm Way #3", "34241", "pat@example.com")
    assert o.notes == ""  # "Unit Notes" was set to Don't import


def test_first_and_last_name_columns_combine(client):
    manager(client)
    walk(client, "First,Last,Address,ZIP\nPat,Owner,1 A St,34241\n", {"First": "first", "Last": "last", "Address": "property_address", "ZIP": "zip_code"})
    assert Owner.objects.get().name == "Pat Owner"


def test_missing_required_matches_and_duplicates_are_explained(client):
    manager(client)
    r = client.post("/import/", {"action": "upload", "file": SimpleUploadedFile("o.csv", ODD.encode())})
    cols = r.context["columns"]
    post = {f"map_{i}": cur for i, h, _e, cur in cols}  # no address matched
    page = client.post("/import/", {"action": "map", **post}).content.decode()
    assert "Step 2 of 3" in page and "Match a column to Property address" in page
    post2 = {f"map_{i}": "property_address" if h in ("Where", "Unit Notes") else cur for i, h, _e, cur in cols}
    assert "More than one column is matched to: Address (physical)" in client.post("/import/", {"action": "map", **post2}).content.decode()
    assert Owner.objects.count() == 0


def test_the_matching_is_remembered_for_the_next_file_with_the_same_headers(client):
    manager(client)
    walk(client, ODD, {"Person": "name", "Where": "property_address", "Zone": "zip_code", "Reach": "email"})
    r = upload(client, ODD.replace("Pat Owner", "Sam Owner").replace("12 Palm", "14 Palm"))
    assert "I remembered your choices" in r.content.decode()
    assert {h: cur for _i, h, _e, cur in r.context["columns"]}["Where"] == "property_address"


def test_apply_with_invitations_and_cancel(client):
    manager(client)
    mail.outbox.clear()
    walk(client, CSV, invite=True)
    assert Owner.objects.count() == 1 and len(mail.outbox) == 1 and Profile.objects.filter(role="homeowner").count() == 1
    upload(client, CSV)
    client.post("/import/", {"action": "cancel"})
    assert "Step 2" not in client.get("/import/").content.decode()
    assert client.post("/import/", {"action": "apply"}).status_code == 302  # nothing waiting: sent back to upload


def test_matching_steps_are_staff_only(client):
    u = U.objects.create_user("cmte", password=PW)
    Profile.objects.create(user=u, role="committee")
    client.force_login(u)
    assert upload(client, CSV).status_code == 403
    assert client.post("/import/", {"action": "apply"}).status_code == 403


def test_excel_and_mac_csv_go_through_the_new_flow(client):
    manager(client)
    walk(client, BASE.replace("\n", "\r").encode())
    assert Owner.objects.count() == 2
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["Owner Name", "Property Address", "ZIP"])
    wb.active.append(["Lee", "9 Bay St", 34241])
    buf = io.BytesIO()
    wb.save(buf)
    walk(client, buf.getvalue())
    assert Owner.objects.get(name="Lee").zip_code == "34241"


def test_map_tiles_are_configurable_send_a_referer_and_have_a_backup(client, settings):
    manager(client)
    r = client.get("/map/")
    body = r.content.decode()
    assert r["Referrer-Policy"] == "strict-origin-when-cross-origin"  # OpenStreetMap blocks tile requests without one
    assert "tile.openstreetmap.org" in body and "basemaps.cartocdn.com" in body and "referrerPolicy" in body and "tileerror" in body
    settings.MAP_TILE_URL = "https://tiles.example/{z}/{x}/{y}.png?key=K"
    assert "tiles.example" in client.get("/map/").content.decode()


# ---------- map filters ----------
def test_every_status_has_a_map_stage_and_every_stage_is_offered():
    from violations import views
    assert {st for st in Violation.Status.values if st != "draft"} <= set(views.STAGE_OF)
    assert set(views.STAGE_OF.values()) == {k for k, _ in views.MAP_STAGES}


def test_map_api_gives_each_case_a_stage_and_issue_date(client):
    from violations.models import Notice
    manager(client)
    o = Owner.objects.create(name="A", property_address="1 A St", lat=27.3, lng=-82.5)
    t = ViolationType.objects.create(name="Shed")
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status="pending_board")
    n = Notice.objects.create(violation=v, kind="violation")
    Notice.objects.filter(pk=n.pk).update(created_at=__import__("django.utils.timezone", fromlist=["now"]).now() - __import__("datetime").timedelta(days=45))
    case = client.get("/api/map/").json()["homes"][0]["cases"][0]
    assert case["stage"] == "board" and case["date"] == (date.today() - __import__("datetime").timedelta(days=45)).isoformat()


def test_map_page_has_the_stage_checkboxes_and_date_ranges(client):
    manager(client)
    page = client.get("/map/").content.decode()
    for label in ["Notice issued / cure period", "Board review / fine issued by Board", "Sent to Compliance Committee / hearing",
                  "Fine imposed &amp; posted to ledger", "On hold", "Sent to legal", "Resolved / closed"]:
        assert label in page, label
    assert page.count('class="stage"') == 7 and page.count("checked>") >= 7
    for d in (30, 60, 90, 180):
        assert f'<option value="{d}">{d} days</option>' in page
    assert "violations/mapfilter.js" in page and 'id="show-empty"' in page
    from pathlib import Path
    assert (Path(__file__).resolve().parents[1] / "static" / "violations" / "mapfilter.js").exists()


def test_filter_logic_in_javascript():
    """Runs the real mapfilter.js under node (skipped where node isn't installed)."""
    import json
    import pathlib
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = pathlib.Path(__file__).resolve().parents[1] / "static" / "violations" / "mapfilter.js"
    script = r"""
const F = require(process.argv[1]);
const now = new Date("2026-11-17T12:00:00").getTime();
const day = n => new Date(now - n*86400000).toISOString().slice(0,10);
const all = ["cure","board","committee","imposed","hold","legal","resolved"];
const home = (...cs) => ({cases: cs.map(([stage, age], i) => ({id:i, stage, date: day(age)}))});
const f = (o) => Object.assign({stages: all, days: 0, nowMs: now, showEmpty: true}, o);
const out = {};
// colors come from the matching cases only
const h = home(["cure", 10], ["committee", 200], ["resolved", 5]);
out.allRed = F.homeView(h, f({})).color;                                   // committee case => red
out.cureOnly = F.homeView(h, f({stages:["cure"]})).color;                  // orange
out.resolvedOnly = F.homeView(h, f({stages:["resolved"]})).color;          // green
out.hold = F.homeView(home(["hold", 1]), f({})).color;                     // orange
out.legal = F.homeView(home(["legal", 1]), f({})).color;                   // red
out.none = F.homeView(h, f({stages:[]})).visible;                          // no stage ticked => hidden
// multi-select
out.two = F.homeView(h, f({stages:["cure","resolved"]})).cases.length;     // 2
// date ranges: issued in the LAST n days, boundary inclusive
const edge = home(["cure", 30]);
out.d30 = F.homeView(edge, f({days:30})).visible; out.d30b = F.homeView(home(["cure",31]), f({days:30})).visible;
out.d60 = F.homeView(home(["cure",60]), f({days:60})).visible; out.d60b = F.homeView(home(["cure",61]), f({days:60})).visible;
out.d90 = F.homeView(home(["cure",90]), f({days:90})).visible; out.d180 = F.homeView(home(["cure",180]), f({days:180})).visible;
out.d180b = F.homeView(home(["cure",181]), f({days:180})).visible; out.allTime = F.homeView(home(["cure",3000]), f({days:0})).visible;
// hidden count and empty homes
out.hidden = F.homeView(h, f({stages:["cure"]})).hidden;                   // 2 other cases hidden
out.emptyShown = F.homeView({cases:[]}, f({})).visible; out.emptyHidden = F.homeView({cases:[]}, f({showEmpty:false})).visible;
out.emptyGray = F.homeView({cases:[]}, f({})).color;
out.homeWithCasesNotMatching = F.homeView(h, f({stages:["legal"]})).visible;   // has cases, none match => hidden (not "empty")
// counts honour the age filter
out.counts = F.stageCounts([h, home(["cure", 100])], 90, now);
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", script, str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert (o["allRed"], o["cureOnly"], o["resolvedOnly"], o["hold"], o["legal"]) == ("red", "orange", "green", "orange", "red")
    assert o["none"] is False and o["two"] == 2
    assert (o["d30"], o["d30b"], o["d60"], o["d60b"], o["d90"], o["d180"], o["d180b"], o["allTime"]) == (True, False, True, False, True, True, False, True)
    assert o["hidden"] == 2 and o["emptyShown"] is True and o["emptyHidden"] is False and o["emptyGray"] == "gray"
    assert o["homeWithCasesNotMatching"] is False
    assert o["counts"] == {"cure": 1, "resolved": 1}  # the 200-day committee case and the 100-day cure case fall outside 90 days


def test_map_has_view_and_label_toggles_and_satellite_imagery(client, settings):
    manager(client)
    page = client.get("/map/").content.decode()
    for must in ['name="view" value="plan"', 'name="view" value="street"', 'name="view" value="satellite"', "Site plan", "Satellite",
                 'id="lbl-address"', 'id="lbl-lot"', "Lot no.", "World_Imagery", "sat_url"]:
        assert must in page, must
    settings.MAP_SATELLITE_URL = "https://sat.example/{z}/{x}/{y}.jpg"
    assert "sat.example" in client.get("/map/").content.decode()


def test_map_api_includes_the_lot_number(client):
    manager(client)
    Owner.objects.create(name="A", property_address="6632 Dovekey Drive #13", lot_no="13", lat=27.3, lng=-82.5)
    assert client.get("/api/map/").json()["homes"][0]["lot"] == "13"


def test_label_text_in_javascript():
    import json
    import pathlib
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = pathlib.Path(__file__).resolve().parents[1] / "static" / "violations" / "mapfilter.js"
    script = r"""
const F = require(process.argv[1]);
const h = {address: "6632 Dovekey Drive #13", lot: "13"};
const out = [F.shortAddress("6632 Dovekey Drive #13","13"), F.shortAddress("7233 Great Egret Boulevard #8","8"), F.shortAddress("6809 Dunlin Court #68","68"),
  F.shortAddress("100 Main Street Unit 5","5"), F.labelText(h,{address:true,lot:false,short:true}), F.labelText(h,{address:true,lot:true}), F.labelText(h,{address:true,lot:false}), F.labelText(h,{address:false,lot:true}),
  F.labelText(h,{address:false,lot:false}), F.labelText({address:"1 A St", lot:""},{address:true,lot:true}), F.labelText({address:"1 A St", lot:""},{address:false,lot:true})];
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", script, str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["6632 Dovekey Dr", "7233 Great Egret Blvd", "6809 Dunlin Ct", "100 Main St Unit 5", "6632 Dovekey Dr",
                                    "Lot 13 · 6632 Dovekey Drive #13", "6632 Dovekey Drive #13", "Lot 13", "", "1 A St", ""]


# ---------- the site plan view ----------
def test_site_plan_assets_cover_all_95_lots():
    import json
    from pathlib import Path
    base = Path(__file__).resolve().parents[1] / "static" / "violations"
    data = json.loads((base / "site-plan-lots.json").read_text())
    assert (base / "site-plan.png").stat().st_size > 50_000 and data["width"] > 1000 and data["height"] > 1000
    assert sorted(map(int, data["lots"])) == list(range(1, 96))
    assert all(0 < x < 1 and 0 < y < 1 for x, y in data["lots"].values())
    # spot checks against the plat: lot 1 is at the top (north), lot 57 at the bottom (Clark Rd), lot 21 at the far west
    L = data["lots"]
    assert L["1"][1] < L["50"][1] < L["57"][1] and L["21"][0] == min(x for x, _ in L.values())


def test_map_page_carries_the_site_plan(client):
    manager(client)
    page = client.get("/map/").content.decode()
    assert 'id="plan-config"' in page and "site-plan.png" in page and 'id="plan"' in page and "artist&#x27;s rendering" in page.replace("'", "&#x27;")
    assert '"57"' in page  # the lot positions are embedded for the page's script


def test_homes_without_street_coordinates_still_reach_the_site_plan(client):
    """The first-time situation: owners imported, 'Locate homes' not run yet. The site plan must still show every home with a lot."""
    manager(client)
    for lot in (1, 2, 3):
        Owner.objects.create(name=f"O{lot}", property_address=f"{lot} Egret Blvd #{lot}", lot_no=str(lot))   # no lat / lng
    Owner.objects.create(name="Gone", property_address="9 Old St", lot_no="9", active=False)
    homes = client.get("/api/map/").json()["homes"]
    assert sorted(h["lot"] for h in homes) == ["1", "2", "3"]                      # active homes only
    assert all(h["lat"] is None and h["lng"] is None for h in homes)               # no crash while spreading shared coordinates
    page = client.get("/map/").content.decode()
    assert "3 home(s) are not on the street / satellite maps yet" in page
