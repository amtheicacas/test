"""The Vantaca 'homeowner report' fields: Homeowner ID, Account Number, Homeowner(s), Address, Mailing Address(+1), second owner,
';'-separated emails, tenants and lease dates."""
import io
import shutil
import subprocess
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from violations import accounts, ledger, mailing, services, vantaca
from violations.models import Notice, Owner, Photo, Violation, ViolationType
from violations.pdf import render_notice_pdf

S = Violation.Status
pytestmark = pytest.mark.django_db
TODAY = date.today()

HEADERS = ("Homeowner ID,Account Number,Homeowner(s),Address,Mailing Address,Street No,Address 1,City,State,Zip,First Name,Last Name,"
           "Second Owner First Name,Second Owner Last Name,Mailing Address 1,Lot No,Phone,Email,Tenant Name(s),Tenant Email(s),"
           "Tenant Phone(s),Tenant Min Start Date,Tenant Min End Date")


def row(**kw):
    base = dict.fromkeys(HEADERS.split(","), "")
    base.update({"Homeowner ID": "H1", "Account Number": "A-1", "Homeowner(s)": "Pat Owner", "Address": "12 Palm Way #3", "City": "Sarasota",
                 "State": "FL", "Zip": "34241", "Email": "pat@example.com"})
    base.update({k.replace("_", " "): v for k, v in kw.items()})
    return base


def load(*rows):
    return vantaca.sync_rows(list(rows))


@pytest.fixture(autouse=True)
def _s(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def png():
    b = io.BytesIO()
    Image.new("RGB", (30, 20)).save(b, "PNG")
    return SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png")


def case_for(owner):
    t = ViolationType.objects.get_or_create(name="Shed")[0]
    v = Violation.objects.create(owner=owner, vtype=t, description="d", cure_required="c", status=S.NOTICE_SENT,
                                 cure_deadline=TODAY + timedelta(days=10))
    Photo.objects.create(violation=v, image=png())
    return v


# ---------- column matching understands the report's own headers ----------
def test_every_report_column_is_recognised_automatically():
    guess = vantaca.auto_mapping(HEADERS.split(","))
    assert guess == {
        "Homeowner ID": "external_id", "Account Number": "account_number", "Homeowner(s)": "name", "Address": "property_address",
        "Mailing Address": "mailing_address", "Street No": "street_no", "Address 1": "address1", "City": "city", "State": "state",
        "Zip": "zip_code", "First Name": "first", "Last Name": "last", "Second Owner First Name": "second_first",
        "Second Owner Last Name": "second_last", "Mailing Address 1": "mailing_address1", "Lot No": "lot_no", "Phone": "phone",
        "Email": "email", "Tenant Name(s)": "tenant_name", "Tenant Email(s)": "tenant_email", "Tenant Phone(s)": "tenant_phone",
        "Tenant Min Start Date": "tenant_start", "Tenant Min End Date": "tenant_end"}


# ---------- IDs, names, address ----------
def test_ids_names_lot_and_address_are_stored():
    load(row(Lot_No="52", First_Name="Pat", Last_Name="Owner"))
    o = Owner.objects.get()
    assert (o.external_id, o.account_number, o.name, o.lot_no, o.property_address) == ("H1", "A-1", "Pat Owner", "52", "12 Palm Way #3")


def test_second_owner_is_added_to_the_name_on_letters():
    load(row(Second_Owner_First_Name="Sam", Second_Owner_Last_Name="Owner"))
    o = Owner.objects.get()
    assert o.second_owner_name == "Sam Owner" and o.display_name == "Pat Owner & Sam Owner"
    assert "Pat Owner & Sam Owner" in o.address_block("property") and o.name == "Pat Owner"


def test_second_owner_already_in_the_first_name_is_not_repeated():
    load(row(**{"Homeowner(s)": "Pat Owner & Sam Owner", "Second_Owner_First_Name": "Sam", "Second_Owner_Last_Name": "Owner"}))
    assert Owner.objects.get().display_name == "Pat Owner & Sam Owner"


def test_first_and_last_name_are_used_when_homeowners_is_blank():
    load(row(**{"Homeowner(s)": "", "First_Name": "Pat", "Last_Name": "Owner"}))
    assert Owner.objects.get().name == "Pat Owner"


def test_address_is_built_from_street_no_and_address_1_when_blank():
    load(row(Address="", Street_No="7335", Address_1="Great Egret Blvd #52"))
    assert Owner.objects.get().property_address == "7335 Great Egret Blvd #52"


def test_matching_prefers_homeowner_id_then_account_number_then_address():
    load(row())
    load(row(**{"Account Number": "A-1", "Homeowner ID": "H1", "Homeowner(s)": "Pat Q Owner", "Address": "99 Moved Ln"}))
    assert Owner.objects.count() == 1 and Owner.objects.get().name == "Pat Q Owner"  # found by Homeowner ID despite a new address
    load(row(**{"Homeowner ID": "", "Account Number": "A-1", "Homeowner(s)": "Pat R Owner", "Address": "98 Other Ln"}))
    assert Owner.objects.count() == 1 and Owner.objects.get().name == "Pat R Owner"  # found by Account Number
    load(row(**{"Homeowner ID": "", "Account Number": "", "Homeowner(s)": "Pat S Owner", "Address": "98 other ln"}))
    assert Owner.objects.count() == 1 and Owner.objects.get().name == "Pat S Owner"  # found by address


# ---------- several emails in one field ----------
def test_semicolon_separated_emails_all_get_violation_emails():
    load(row(Email="pat@example.com; pat.work@example.com ;sam@example.com"))
    o = Owner.objects.get()
    assert o.email == "pat@example.com" and o.additional_emails == "pat.work@example.com;sam@example.com"
    assert o.all_emails == ["pat@example.com", "pat.work@example.com", "sam@example.com"]
    v = case_for(o)
    mail.outbox.clear()
    services.send_notice(v, "violation")
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["pat@example.com", "pat.work@example.com", "sam@example.com"]
    assert mail.outbox[0].body.startswith("Dear Pat Owner,")


def test_any_listed_email_can_get_access(client):
    load(row(Email="pat@example.com;pat.work@example.com"))
    client.post("/accounts/get-access/", {"email": "pat.work@example.com"})
    assert accounts.household_for_email("pat.work@example.com") == Owner.objects.get()
    assert accounts.household_for_email("pat.wor@example.com") is None  # no partial matches
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["pat.work@example.com"]


def test_bad_emails_are_flagged_in_the_preview():
    problems = vantaca.check_rows([row(Email="pat@example.com;nope")])
    assert any("'nope' doesn't look like a valid email" in m for _, m in problems)


# ---------- mailing address (Vantaca's layout: Address includes city/state/zip; Mailing Address is a street line only) ----------
def test_address_with_city_state_zip_is_cleaned_to_the_street():
    load(row(Address="6632 Dovekey Drive #13 Sarasota, FL 34241", City="Sarasota", State="FL", Zip="34241"))
    o = Owner.objects.get()
    assert o.property_address == "6632 Dovekey Drive #13" and o.address_block("property").endswith("6632 Dovekey Drive #13\nSarasota, FL 34241")


def test_mailing_that_is_just_the_property_street_is_not_a_second_address():
    load(row(Address="6632 Dovekey Drive #13 Sarasota, FL 34241", Mailing_Address="6632 Dovekey Drive #13", Mailing_Address_1="Dovekey Drive #13"))
    o = Owner.objects.get()
    assert not o.has_separate_mailing and o.mailing_address == "" and o.owner_mail_kinds() == ["property"]
    assert vantaca.check_rows([row(Address="6632 Dovekey Drive #13 Sarasota, FL 34241", Mailing_Address="6632 Dovekey Drive #13")]) == []


def test_mailing_address_1_is_not_glued_onto_the_mailing_address():
    load(row(Mailing_Address="9180 Swing Branch Road", Mailing_Address_1="Swing Branch Road"))
    assert Owner.objects.get().mailing_address == "9180 Swing Branch Road"  # not "9180 Swing Branch Road\nSwing Branch Road"


def test_out_of_town_owner_with_only_a_street_line_is_flagged_not_dropped():
    load(row(Address="6809 Dunlin Court #68 Sarasota, FL 34241", Mailing_Address="9180 Swing Branch Road.", Mailing_Address_1="Swing Branch Road."))
    o = Owner.objects.get()
    assert o.has_separate_mailing and o.mailing_address == "9180 Swing Branch Road" and o.mail_city == ""  # trailing period dropped
    problems = vantaca.check_rows([row(Mailing_Address="PO Box 636")])
    assert len(problems) == 1 and "no city, state and ZIP" in problems[0][1]


def test_mailing_city_state_zip_columns_complete_the_address_when_the_report_has_them():
    load(row(**{"Mailing Address": "PO Box 636", "Mailing City": "Tampa", "Mailing State": "FL", "Mailing Zip": "33601"}))
    o = Owner.objects.get()
    assert (o.mail_line1, o.mail_city, o.mail_state, o.mail_zip) == ("PO Box 636", "Tampa", "FL", "33601")
    assert vantaca.check_rows([row(**{"Mailing Address": "PO Box 636", "Mailing City": "Tampa", "Mailing State": "FL", "Mailing Zip": "33601"})]) == []


def test_a_full_mailing_address_in_one_cell_is_split():
    load(row(Mailing_Address="PO Box 55, Tampa, FL 33601"))
    o = Owner.objects.get()
    assert (o.mail_line1, o.mail_city, o.mail_state, o.mail_zip) == ("PO Box 55", "Tampa", "FL", "33601") and o.has_separate_mailing


def test_repeated_problems_are_shown_once_with_their_rows():
    problems = vantaca.check_rows([row(**{"Homeowner ID": f"H{i}", "Address": f"{i} A St", "Mailing Address": f"{i} Far Rd"}) for i in range(1, 41)])
    groups = vantaca.group_problems(problems)
    assert len(groups) == 1 and groups[0][1] == list(range(1, 41))


@pytest.mark.parametrize("text,expected", [
    ("100 Main St, Apt 4, Tampa, FL 33601-1234", ("100 Main St, Apt 4", "Tampa", "FL", "33601")),
    ("100 Main St\nSt. Petersburg FL 33701", ("100 Main St", "St. Petersburg", "FL", "33701")),  # a line break works as the comma
    ("100 Main St\nTampa, FL 33601", ("100 Main St", "Tampa", "FL", "33601")),
    ("PO Box 9, New York, ny 10001", ("PO Box 9", "New York", "NY", "10001")),
    ("just a street", None),
])
def test_us_address_parsing(text, expected):
    got = vantaca.parse_us_address(text)
    assert (got and (got["line1"], got["city"], got["state"], got["zip"])) == (expected or None)


def test_mailing_same_as_property_is_not_a_second_address():
    load(row(Mailing_Address="12 Palm Way #3, Sarasota, FL 34241"))
    assert not Owner.objects.get().has_separate_mailing


def test_a_blank_mailing_column_clears_an_old_mailing_address():
    load(row(Mailing_Address="PO Box 55, Tampa, FL 33601"))
    load(row())
    o = Owner.objects.get()
    assert not o.has_separate_mailing and o.mail_line1 == ""


def _lob(settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY, settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "lob", "k", "1 Way", "34240"


def test_us_mail_goes_to_the_mailing_address_and_the_property_address(settings):
    _lob(settings)
    load(row(Mailing_Address="PO Box 55, Tampa, FL 33601"))
    v = case_for(Owner.objects.get())
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: sent.append((to["line1"], to["city"])) or {"id": "x"}):
        services.send_notice(v, "violation")
    assert sent == [("PO Box 55", "Tampa"), ("12 Palm Way #3", "Sarasota")]


def test_hearing_notice_is_certified_to_both_addresses_and_the_tenant(settings):
    _lob(settings)
    load(row(Mailing_Address="PO Box 55, Tampa, FL 33601", Tenant_Name_s_="Tina", **{"Tenant Name(s)": "Tina"}))
    v = case_for(Owner.objects.get())
    v.status, v.proposed_fine, v.hearing_date = S.HEARING_SCHEDULED, 100, __import__("django.utils.timezone", fromlist=["now"]).now()
    v.save()
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: sent.append((to["name"], to["line1"], extra)) or {"id": "x"}):
        services.send_notice(v, "hearing")
    assert [(n, a) for n, a, _ in sent] == [("Pat Owner", "PO Box 55")] * 2 + [("Pat Owner", "12 Palm Way #3")] * 2 + [("Tina", "12 Palm Way #3")] * 2
    assert [e for *_, e in sent] == ["certified_return_receipt", None] * 3


def test_an_incomplete_mailing_address_does_not_stop_the_property_mailing(settings):
    _lob(settings)
    load(row(Mailing_Address="9180 Swing Branch Road"))  # street line only: no city/state/ZIP
    v = case_for(Owner.objects.get())
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: sent.append(to["line1"]) or {"id": "x"}):
        n = services.send_notice(v, "violation")
    n.refresh_from_db()
    assert sent == ["12 Palm Way #3"]  # only the property copy could be sent
    assert "incomplete mailing address" in n.provider_note and n.mailed_on is not None  # and the problem is on the notice


def _text(pdf):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    return " ".join(subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True).stdout.decode().split())


def test_letters_for_each_address_carry_that_address_and_both_names():
    load(row(Mailing_Address="PO Box 55, Tampa, FL 33601", Second_Owner_First_Name="Sam", Second_Owner_Last_Name="Owner"))
    n = Notice.objects.create(violation=case_for(Owner.objects.get()), kind="violation")
    mailing_copy, property_copy = _text(render_notice_pdf(n, "owner")), _text(render_notice_pdf(n, "owner_property"))
    assert "Pat Owner & Sam Owner PO Box 55, Tampa, FL 33601" in mailing_copy and "Dear Pat Owner & Sam Owner:" in mailing_copy
    assert "Pat Owner & Sam Owner 12 Palm Way #3 Sarasota, FL 34241" in property_copy


def test_staff_get_a_print_link_for_each_address(client):
    from django.contrib.auth import get_user_model
    from violations.models import Profile
    load(row(Mailing_Address="PO Box 55, Tampa, FL 33601", **{"Tenant Name(s)": "Tina"}))
    v = case_for(Owner.objects.get())
    n = services.send_notice(v, "violation")
    u = get_user_model().objects.create_user("m", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="manager")
    client.force_login(u)
    page = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Print (owner, mailing address)" in page and "Print (owner, property address)" in page and "Print (tenant)" in page
    for to in ("owner_property", "tenant"):
        assert client.get(f"/notices/{n.pk}/pdf/?to={to}").content.startswith(b"%PDF")


# ---------- tenants and lease dates ----------
def test_tenant_fields_emails_and_dates_are_read():
    load(row(**{"Tenant Name(s)": "Tina & Tom Tenant", "Tenant Email(s)": "tina@example.com; tom@example.com", "Tenant Phone(s)": "941-555-1",
                "Tenant Min Start Date": "01/15/2026", "Tenant Min End Date": (TODAY + timedelta(days=200)).strftime("%m/%d/%Y")}))
    o = Owner.objects.get()
    assert o.tenant_name == "Tina & Tom Tenant" and o.tenant_emails == ["tina@example.com", "tom@example.com"]
    assert o.tenant_start == date(2026, 1, 15) and o.has_tenant


@pytest.mark.parametrize("text,expected", [("11/05/2026", date(2026, 11, 5)), ("11/5/26", date(2026, 11, 5)), ("2026-11-05", date(2026, 11, 5)),
                                           ("2026-11-05 00:00:00", date(2026, 11, 5)), ("Nov 5, 2026", date(2026, 11, 5)),
                                           ("November 5, 2026", date(2026, 11, 5)), ("", None)])
def test_dates_in_common_formats(text, expected):
    assert vantaca.parse_date(text) == (expected, True)
    assert vantaca.parse_date("someday")[1] is False


def test_tenant_stops_getting_notices_when_the_lease_ends_or_before_it_starts():
    load(row(**{"Tenant Name(s)": "Tina", "Tenant Email(s)": "tina@example.com", "Tenant Min Start Date": "01/01/2020",
                "Tenant Min End Date": (TODAY - timedelta(days=1)).strftime("%m/%d/%Y")}))
    o = Owner.objects.get()
    assert not o.has_tenant and o.tenant_lease_ended
    v = case_for(o)
    mail.outbox.clear()
    services.send_notice(v, "violation")
    assert [m.to for m in mail.outbox] == [["pat@example.com"]] and not v.non_owner_occupied
    o.tenant_start, o.tenant_end = TODAY + timedelta(days=30), None
    o.save()
    assert not o.has_tenant  # lease hasn't started yet
    o.tenant_start = TODAY - timedelta(days=1)
    o.save()
    assert o.has_tenant and v.non_owner_occupied


def test_lease_with_no_dates_counts_as_current():
    load(row(**{"Tenant Name(s)": "Tina", "Tenant Email(s)": "tina@example.com"}))
    assert Owner.objects.get().has_tenant


def test_preview_warns_about_an_ended_lease_and_unreadable_dates():
    old = (TODAY - timedelta(days=30)).strftime("%m/%d/%Y")
    problems = vantaca.check_rows([row(**{"Tenant Name(s)": "Tina", "Tenant Min End Date": old}),
                                   row(**{"Homeowner ID": "H2", "Address": "2 B St", "Tenant Name(s)": "Tom", "Tenant Min End Date": "soon"})])
    text = " ".join(m for _, m in problems)
    assert "lease ended" in text and "Couldn't read the date 'soon'" in text


def test_a_blank_tenant_clears_the_tenant_and_missing_columns_leave_it_alone():
    load(row(**{"Tenant Name(s)": "Tina", "Tenant Email(s)": "tina@example.com"}))
    vantaca.sync_rows([{"Homeowner ID": "H1", "Homeowner(s)": "Pat Owner", "Address": "12 Palm Way #3", "Zip": "34241"}])  # no tenant columns
    assert Owner.objects.get().tenant_name == "Tina"
    load(row())  # tenant columns present but blank: moved out
    o = Owner.objects.get()
    assert o.tenant_name == "" and o.tenant_emails == [] and o.tenant_end is None


# ---------- ledger account ----------
def test_ledger_uses_the_account_number_by_default(settings):
    load(row())
    o = Owner.objects.get()
    assert ledger.account_id(o) == "A-1"
    settings.VANTACA_LEDGER_ACCOUNT_FIELD = "external_id"
    assert ledger.account_id(o) == "H1"


# ---------- the template round-trips ----------
def test_the_downloaded_template_imports_cleanly(client):
    from django.contrib.auth import get_user_model
    from violations.models import Profile
    u = get_user_model().objects.create_user("m", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="manager")
    client.force_login(u)
    t = client.get("/import/template.csv").content
    rows = vantaca.rows_from_upload(t)
    _, result, problems = vantaca.preview_rows(rows)
    assert result["created"] == 1 and problems == []


# ---------- the street is built from its pieces ----------
def test_street_is_built_from_street_no_and_address_1():
    load(row(Address="6632 Dovekey Drive #13 Sarasota, FL 34241", Street_No="6632", Address_1="Dovekey Drive #13"))
    assert Owner.objects.get().property_address == "6632 Dovekey Drive #13"


def test_pieces_win_and_a_disagreement_with_the_full_address_is_flagged():
    r = row(Address="6632 Dovekey Drive #13 Sarasota, FL 34241", Street_No="6634", Address_1="Dovekey Drive #13")
    load(r)
    assert Owner.objects.get().property_address == "6634 Dovekey Drive #13"
    assert any("Using '6634 Dovekey Drive #13'" in m for _, m in vantaca.check_rows([r]))
    agree = row(Address="6632 Dovekey Drive #13 Sarasota, FL 34241", Street_No="6632", Address_1="Dovekey Drive #13")
    assert not any("Street No" in m for _, m in vantaca.check_rows([agree]))


def test_full_address_alone_still_works_when_there_are_no_pieces():
    load(row(Address="6632 Dovekey Drive #13 Sarasota, FL 34241"))
    assert Owner.objects.get().property_address == "6632 Dovekey Drive #13"


# ---------- one missing lot number ----------
def lot_rows(blank_at=None, lots=None, n=10, strip_unit=True):
    rows = []
    for i in range(1, n + 1):
        lot = (lots or {}).get(i, str(i))
        r = row(**{"Homeowner ID": f"H{i}", "Account Number": f"A-{i}", "Address": f"{7200 + 4 * i} Egret Blvd #{i} Sarasota, FL 34241", "Street No": str(7200 + 4 * i),
                   "Address 1": f"Egret Blvd #{i}", "Lot No": lot})
        rows.append(r)
    if blank_at:
        rows[blank_at - 1]["Lot No"] = ""
        if strip_unit:
            rows[blank_at - 1]["Address 1"] = "Egret Blvd"
            rows[blank_at - 1]["Address"] = f"{7200 + 4 * blank_at} Egret Blvd Sarasota, FL 34241"
    return rows


def test_the_one_blank_lot_gets_the_one_missing_number():
    rows = lot_rows(blank_at=4)
    filled, notes = vantaca.infer_lots(rows)
    assert filled[3]["Lot No"] == "4" and rows[3]["Lot No"] == ""  # the input isn't modified
    assert notes[0] == (4, "Lot no. was blank. Set to 4, the only number missing from lots 1-10.")
    vantaca.sync_rows(rows)
    assert Owner.objects.get(external_id="H4").lot_no == "4"
    assert Owner.objects.exclude(lot_no="").count() == 10


def test_the_address_without_its_unit_is_flagged_not_changed():
    problems = vantaca.check_rows(lot_rows(blank_at=4))
    text = " ".join(m for _, m in problems)
    assert "Set to 4" in text and "this one has no '#4'" in text
    vantaca.sync_rows(lot_rows(blank_at=4))
    assert Owner.objects.get(external_id="H4").property_address == "7216 Egret Blvd"  # left as Vantaca has it


def test_a_blank_lot_is_left_alone_when_it_is_not_clear_which_number_it_is():
    two_blank = lot_rows(blank_at=4)
    two_blank[6]["Lot No"] = ""
    for rows, why in [(two_blank, "more than one Lot No. is blank"),
                      (lot_rows(blank_at=4, lots={5: "12"}), "more than one unused number"),   # 4 and 5 are unused
                      (lot_rows(blank_at=4, lots={6: "3"}), "more than one unused number")]:  # a duplicate 3 leaves 4 and 6
        filled, notes = vantaca.infer_lots(rows)
        assert filled == rows and any(why in m for _, m in notes), why
    vantaca.sync_rows(two_blank)
    assert Owner.objects.filter(lot_no="").count() == 2


def test_no_lot_column_or_no_blank_means_nothing_to_do():
    assert vantaca.infer_lots([{"Homeowner ID": "1", "Homeowner(s)": "A", "Address": "1 A St"}])[1] == []
    rows = lot_rows()
    assert vantaca.infer_lots(rows) == (rows, [])


def test_the_preview_shows_the_lot_fix(client):
    from django.contrib.auth import get_user_model
    from violations.models import Profile
    u = get_user_model().objects.create_user("m", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="manager")
    client.force_login(u)
    import csv as _csv
    buf = io.StringIO()
    rows = lot_rows(blank_at=4)
    w = _csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    r = client.post("/import/", {"action": "upload", "file": SimpleUploadedFile("o.csv", buf.getvalue().encode())})
    post = {f"map_{i}": cur for i, h, _e, cur in r.context["columns"]}
    page = client.post("/import/", {"action": "map", **post}).content.decode()
    assert "Set to 4, the only number missing from lots 1-10" in page
