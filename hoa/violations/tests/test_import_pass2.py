"""The owner import / Vantaca sync with files and rows as they really arrive (second stabilisation pass): Excel's CSV encodings, a stray BOM, line breaks inside
cells, control characters, a row nobody can read, the same home twice, a changed email, an owner who disappears. One bad row must never stop the others, and a
partly read file must never deactivate anyone."""
import io

import pytest
from django.test import Client

from violations import vantaca
from violations.models import Owner, Profile

pytestmark = pytest.mark.django_db
HEAD = "Account ID,Homeowner Name,Property Address,Email Address,Latitude,Longitude\n"


def csv_bytes(rows, head=HEAD, enc="utf-8"):
    return (head + "\n".join(rows) + "\n").encode(enc)


# ---- how the file is read ---------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,delim", [
    (csv_bytes(["A1,José Núñez,1 Palm Way,j@example.com,,"], enc="utf-8-sig"), ","),                                  # Excel "CSV UTF-8": a BOM
    (csv_bytes(["A1,José Núñez,1 Palm Way,j@example.com,,"], enc="cp1252"), ","),                                      # Excel's plain "CSV" on Windows
    (csv_bytes(["A1;José Núñez;1 Palm Way;j@example.com;;"], head=HEAD.replace(",", ";"), enc="cp1252"), ";"),        # European Excel
    (csv_bytes(["A1\tJosé Núñez\t1 Palm Way\tj@example.com\t\t"], head=HEAD.replace(",", "\t")), "\t"),
    (csv_bytes(["A1,José Núñez,1 Palm Way,j@example.com,,"]).replace(b"\n", b"\r\n"), ","),                          # Windows line endings
    (csv_bytes(["A1,José Núñez,1 Palm Way,j@example.com,,"]).replace(b"\n", b"\r"), ","),                            # old Mac line endings
])
def test_the_common_csv_layouts_are_read_with_their_accents(raw, delim):
    rows = vantaca.rows_from_upload(raw, "owners.csv")
    assert len(rows) == 1 and "José" in list(rows[0].values())[1] and "1 Palm Way" in rows[0].values()


@pytest.mark.parametrize("raw", [b"", b"   \n\n", HEAD.encode(), b"\x00\x01\x02binary", b"PK\x03\x04not really a workbook", b"\xd0\xcf\x11\xe0old excel", b"\xff\xfeA\x00,\x00B\x00\n\x00",
                                 b"1,2,3", b"\n\n\n,,,\n"])
def test_a_file_that_is_not_an_owner_list_gets_a_plain_message_never_an_error_page(raw):
    with pytest.raises(vantaca.UploadError):
        vantaca.rows_from_upload(raw, "owners.csv")


def test_a_line_break_inside_a_quoted_cell_stays_inside_that_cell():
    raw = (HEAD + 'A1,"Pat\nOwner",1 Palm Way,p@example.com,,\n').encode()
    rows = vantaca.rows_from_upload(raw, "o.csv")
    assert len(rows) == 1 and rows[0]["Homeowner Name"] == "Pat\nOwner"


# ---- what is saved ------------------------------------------------------------------------------------------------------------------------------------
def test_line_breaks_and_control_characters_in_a_cell_are_cleaned_when_saved():
    rows = vantaca.rows_from_upload((HEAD + 'A1,"Pat\x0b Owner\n","  1 Palm Way\nUnit 2 ",p@example.com,,\n').encode(), "o.csv")
    vantaca.sync_rows(rows)
    o = Owner.objects.get()
    assert (o.name, o.property_address) == ("Pat Owner", "1 Palm Way Unit 2")


def test_a_nul_character_that_arrives_through_the_api_is_removed_not_a_crash_on_postgresql():
    vantaca.sync_rows([{"Account ID": "A1", "Homeowner Name": "Pa\x00t Owner", "Property Address": "1 Palm\x00 Way", "Email Address": "p@exa\x00mple.com"}])
    o = Owner.objects.get()
    assert (o.name, o.property_address, o.email) == ("Pat Owner", "1 Palm Way", "p@example.com")


def test_a_mailing_address_keeps_its_line_breaks_but_not_control_characters():
    rows = [{"Account ID": "A1", "Homeowner Name": "Pat", "Property Address": "1 Palm Way", "Mailing Address": "PO Box 5\nTampa, FL 33601\x0b"}]
    vantaca.sync_rows(rows)
    o = Owner.objects.get()
    assert "\n" in o.mailing_address and "\x0b" not in o.mailing_address and o.mail_city == "Tampa"


def test_a_row_nobody_can_read_does_not_stop_the_others_and_is_reported():
    rows = vantaca.rows_from_upload(csv_bytes(["A1,Pat One,1 Palm Way,p1@example.com,27.1,-82.4", "A2,Pat Two,2 Palm Way,p2@example.com,not-a-number,-82.4",
                                               "A3,Pat Three,3 Palm Way,p3@example.com,27.1,-82.4"]), "o.csv")
    result = vantaca.sync_rows(rows)
    assert (result["created"], result["failed"]) == (3, 0)                         # a bad coordinate is dropped (the street map is optional), not fatal
    assert Owner.objects.get(external_id="A2").lat is None


def test_a_row_that_really_fails_is_counted_and_the_rest_are_saved(monkeypatch):
    real = vantaca.upsert_owner

    def flaky(row):
        if row["Account ID"] == "A2":
            raise ValueError("database refused this row")
        return real(row)
    monkeypatch.setattr(vantaca, "upsert_owner", flaky)
    rows = vantaca.rows_from_upload(csv_bytes(["A1,Pat One,1 Palm Way,,,", "A2,Pat Two,2 Palm Way,,,", "A3,Pat Three,3 Palm Way,,,"]), "o.csv")
    result = vantaca.sync_rows(rows)
    assert (result["created"], result["failed"]) == (2, 1)
    assert set(Owner.objects.values_list("external_id", flat=True)) == {"A1", "A3"}
    from violations.models import AuditLog
    assert AuditLog.objects.filter(action="owners.row_failed").count() == 1


def test_a_partly_unreadable_feed_never_deactivates_anyone(monkeypatch):
    vantaca.sync_rows(vantaca.rows_from_upload(csv_bytes([f"A{i},Pat {i},{i} Palm Way,,," for i in range(1, 11)]), "o.csv"))
    real = vantaca.upsert_owner
    monkeypatch.setattr(vantaca, "upsert_owner", lambda row: (_ for _ in ()).throw(ValueError("bad")) if row["Account ID"] == "A2" else real(row))
    rows = vantaca.rows_from_upload(csv_bytes([f"A{i},Pat {i},{i} Palm Way,,," for i in range(1, 10)]), "o.csv")            # A10 really is missing; A2 is unreadable
    with pytest.raises(vantaca.SafetyStop, match="could not be read"):
        vantaca.sync_rows(rows, deactivate_missing=True)
    assert Owner.objects.filter(active=False).count() == 0


def test_the_same_home_twice_in_one_file_is_one_owner():
    rows = vantaca.rows_from_upload(csv_bytes(["A1,Pat One,1 Palm Way,a@example.com,,", "A1,Pat One Again,1 Palm Way,b@example.com,,"]), "o.csv")
    result = vantaca.sync_rows(rows)
    assert (result["created"], result["updated"]) == (1, 1) and Owner.objects.count() == 1 and Owner.objects.get().email == "b@example.com"


def test_a_changed_email_updates_the_owner_and_the_audit_log_says_so():
    vantaca.sync_rows(vantaca.rows_from_upload(csv_bytes(["A1,Pat One,1 Palm Way,old@example.com,,"]), "o.csv"))
    vantaca.sync_rows(vantaca.rows_from_upload(csv_bytes(["A1,Pat One,1 Palm Way,new@example.com,,"]), "o.csv"))
    assert Owner.objects.get().email == "new@example.com"
    from violations.models import AuditLog
    assert AuditLog.objects.filter(action="owner.changed", detail__contains="new@example.com").exists()


def test_an_owner_missing_from_a_full_feed_is_deactivated_but_not_when_it_looks_like_a_partial_feed():
    vantaca.sync_rows(vantaca.rows_from_upload(csv_bytes([f"A{i},Pat {i},{i} Palm Way,,," for i in range(1, 11)]), "o.csv"))
    feed = vantaca.rows_from_upload(csv_bytes([f"A{i},Pat {i},{i} Palm Way,,," for i in range(1, 10)]), "o.csv")
    assert vantaca.sync_rows(feed, deactivate_missing=True)["deactivated"] == 1
    with pytest.raises(vantaca.SafetyStop, match="over 20%"):
        vantaca.sync_rows(feed[:5], deactivate_missing=True)
    assert Owner.objects.filter(active=False).count() == 1


def test_a_large_file_imports_in_reasonable_time():
    import time
    rows = vantaca.rows_from_upload(csv_bytes([f"A{i},Owner {i},{i} Palm Way,o{i}@example.com,27.{i % 99},-82.{i % 99}" for i in range(1, 801)]), "o.csv")
    t = time.time()
    result = vantaca.sync_rows(rows)
    assert result["created"] == 800 and time.time() - t < 60


# ---- the import page ----------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def board_client(django_user_model):
    u = django_user_model.objects.create_user("bd5", password="x")
    Profile.objects.create(user=u, role="board")
    c = Client(raise_request_exception=False)
    c.force_login(u)
    return c


@pytest.mark.parametrize("raw,name", [(b"", "a.csv"), (b"\x00\x01", "a.csv"), (b"PK\x03\x04junk", "a.xlsx"), (b"x" * 200, "a.csv"), (csv_bytes(["A1,Pat,1 Palm Way,,,"], enc="cp1252"), "a.csv")])
def test_uploading_any_file_on_the_import_page_never_gives_an_error_page(board_client, raw, name):
    from django.core.files.uploadedfile import SimpleUploadedFile
    r = board_client.post("/import/", {"action": "upload", "file": SimpleUploadedFile(name, raw)})
    assert r.status_code == 200


def test_the_scheduled_sync_command_ends_with_an_error_when_a_row_could_not_be_imported(tmp_path, monkeypatch):
    from django.core.management import call_command
    from django.core.management.base import CommandError
    f = tmp_path / "owners.csv"
    f.write_bytes(csv_bytes(["A1,Pat One,1 Palm Way,,,", "A2,Pat Two,2 Palm Way,,,"]))
    real = vantaca.upsert_owner
    monkeypatch.setattr(vantaca, "upsert_owner", lambda row: (_ for _ in ()).throw(ValueError("bad")) if row["Account ID"] == "A2" else real(row))
    out = io.StringIO()
    with pytest.raises(CommandError, match="1 row"):
        call_command("sync_vantaca", csv=str(f), stdout=out)
    assert "created 1" in out.getvalue() and Owner.objects.count() == 1


def test_the_import_page_says_when_some_rows_could_not_be_imported(board_client, monkeypatch):
    from django.core.files.uploadedfile import SimpleUploadedFile
    real = vantaca.upsert_owner
    monkeypatch.setattr(vantaca, "upsert_owner", lambda row: (_ for _ in ()).throw(ValueError("bad")) if row.get("homeownerid") == "A2" else real(row))
    board_client.post("/import/", {"action": "upload", "file": SimpleUploadedFile("o.csv", csv_bytes(["A1,Pat One,1 Palm Way,,,", "A2,Pat Two,2 Palm Way,,,"]))})
    mapping = {f"map_{i}": v for i, v in enumerate(["external_id", "name", "property_address", "email", "lat", "lng"])}
    r = board_client.post("/import/", {"action": "apply", **mapping}, follow=True)
    text = r.content.decode()
    assert "1 row(s) could not be imported" in text and Owner.objects.count() == 1
