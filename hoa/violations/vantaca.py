"""Pull homeowner data from Vantaca into Owner records, from a CSV export or the Vantaca API.

Vantaca is the source of truth for name / email / phone / addresses; this app never writes back.
Fields in this app only (notes, violations) are never touched. Owners are matched on Vantaca's ID,
falling back to property address so records created by hand get linked rather than duplicated.

NOTE: the API client is written against Vantaca's documented pattern (HTTP Basic auth, read endpoints,
JSON lists) but could not be tested against a live account. The endpoint path and field names are
configurable; adjust FIELD_ALIASES after you see a real response.
"""
import base64
import csv
import io
import json
import re
import urllib.request
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

from . import accounts
from .models import Owner, split_emails

# our field -> possible column / JSON keys (case-insensitive; spaces, punctuation and underscores ignored).
# The first alias is the canonical name used after the import page's column matching.
FIELD_ALIASES = {
    "external_id": ["homeownerid", "ownerid", "accountid", "id"],
    "account_number": ["accountnumber", "accountno", "acctnumber", "acct"],
    "name": ["name", "ownername", "ownernames", "ownersname", "homeownername", "homeowners", "homeowner", "owners", "owner",
             "fullname", "accountname", "residentname", "resident"],
    "first": ["firstname"], "last": ["lastname"],
    "second_first": ["secondownerfirstname", "secondfirstname", "coownerfirstname", "owner2firstname"],
    "second_last": ["secondownerlastname", "secondlastname", "coownerlastname", "owner2lastname"],
    "property_address": ["address", "propertyaddress", "unitaddress", "streetaddress", "street", "propertystreet", "siteaddress",
                         "situsaddress", "lotaddress", "homeaddress", "property"],
    "street_no": ["streetno", "streetnumber", "housenumber"],
    "address1": ["address1", "streetname"],
    "lot_no": ["lotno", "lotnumber", "lot"],
    "city": ["city", "propertycity"], "state": ["state", "propertystate"],
    "zip_code": ["zip", "zipcode", "postal", "postalcode", "propertyzip"],
    "email": ["email", "emails", "emailaddress", "emailaddresses", "primaryemail", "owneremail", "contactemail"],
    "co_owner_email": ["coowneremail", "coowneremailaddress", "secondaryemail", "email2", "secondownersemail"],
    "phone": ["phone", "phonenumber", "primaryphone", "homephone", "cellphone", "mobile", "telephone"],
    "mailing_address": ["mailingaddress", "mailaddress", "billingaddress", "mailingstreet"],
    "mailing_address1": ["mailingaddress1", "mailaddress1", "mailingaddress2"],
    "mail_city": ["mailingcity"], "mail_state": ["mailingstate"], "mail_zip": ["mailingzip", "mailingzipcode"],
    "lat": ["latitude", "lat"], "lng": ["longitude", "lng", "lon"],
    "tenant_name": ["tenantnames", "tenantname", "tenants", "renter", "rentername", "rentedto", "leasedto", "tenant", "occupantname"],
    "tenant_email": ["tenantemails", "tenantemail", "renteremail", "occupantemail"],
    "tenant_phone": ["tenantphones", "tenantphone", "renterphone"],
    "tenant_start": ["tenantminstartdate", "tenantstartdate", "leasestart", "leasestartdate", "tenantstart"],
    "tenant_end": ["tenantminenddate", "tenantenddate", "leaseend", "leaseenddate", "tenantend"],
}
# Only touched when the file has that column (then a blank means "none", e.g. the tenant moved out or the lease ended).
HOUSEHOLD_FIELDS = ("co_owner_email", "tenant_name", "tenant_email", "tenant_phone")
MAILING_FIELDS = ("mailing_address", "mailing_address1", "mail_city", "mail_state", "mail_zip")


def _norm(k):
    return "".join(c for c in str(k).lower() if c.isalnum())


def _pick(row, field):
    lookup = {_norm(k): v for k, v in row.items()}
    for alias in FIELD_ALIASES[field]:
        v = lookup.get(_norm(alias))
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _has_column(row, field):
    keys = {_norm(k) for k in row}
    return any(_norm(a) in keys for a in FIELD_ALIASES[field])


def _any_column(row, fields):
    return any(_has_column(row, f) for f in fields)


_ADDR = re.compile(r"^(?P<street>.+?),\s*(?P<city>[^,]+?),?\s+(?P<state>[A-Za-z]{2})\.?,?\s+(?P<zip>\d{5})(?:-\d{4})?$")


def parse_us_address(text):
    """'PO Box 5, Tampa, FL 33601' (or the same on several lines) -> {line1, city, state, zip}; None if it can't be split."""
    flat = re.sub(r"\s*[\r\n]+\s*", ", ", (text or "").strip()).strip(", ")
    m = _ADDR.match(flat)
    if not m:
        return None
    return {"line1": m["street"].strip(), "city": m["city"].strip(), "state": m["state"].upper(), "zip": m["zip"]}


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y")


def parse_date(text):
    """(date or None, ok). Blank is fine (None, True); something unreadable is (None, False)."""
    t = (text or "").strip()
    if not t:
        return None, True
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date(), True
        except ValueError:
            pass
    return None, False


def _strip_city_state_zip(street, city, state, zip_code):
    """'6632 Dovekey Drive #13 Sarasota, FL 34241' -> '6632 Dovekey Drive #13' (Vantaca's Address includes city, state and ZIP)."""
    if city and state and zip_code:
        pat = rf",?\s*{re.escape(city)}\b,?\s*{re.escape(state)}\b\.?,?\s*{re.escape(zip_code)}(?:-\d{{4}})?\s*$"
        street = re.sub(pat, "", street, flags=re.I)
    return street.strip(" ,")


def _norm_street(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def normalize(row):
    """One import row -> {field: value} for the Owner, plus '_notes' (things worth a second look)."""
    notes = []
    name = _pick(row, "name") or f"{_pick(row, 'first')} {_pick(row, 'last')}".strip()
    # The street comes from its pieces (Street No + Address 1), which need no parsing; the full "Address" (which also carries
    # city, state and ZIP) is the fallback, and a mismatch between the two is flagged.
    pieces = f"{_pick(row, 'street_no')} {_pick(row, 'address1')}".strip() if _pick(row, "address1") else ""
    full = _strip_city_state_zip(_pick(row, "property_address"), _pick(row, "city"), _pick(row, "state"), _pick(row, "zip_code"))
    street = pieces or full
    if pieces and full and _norm_street(pieces) != _norm_street(full):
        notes.append(f"Address says '{full}' but Street No + Address 1 say '{pieces}'. Using '{pieces}'.")
    emails = split_emails(_pick(row, "email"))
    d = {"external_id": _pick(row, "external_id") or None, "account_number": _pick(row, "account_number"),
         "name": name, "property_address": street, "lot_no": _pick(row, "lot_no"),
         "email": emails[0] if emails else "", "additional_emails": ";".join(emails[1:]), "phone": _pick(row, "phone"),
         **{f: _pick(row, f) for f in ("city", "state", "zip_code")},
         **{f: (float(_pick(row, f)) if _pick(row, f) else None) for f in ("lat", "lng")}}
    if _any_column(row, ("second_first", "second_last")):
        d["second_owner_name"] = f"{_pick(row, 'second_first')} {_pick(row, 'second_last')}".strip()
    for f in HOUSEHOLD_FIELDS:
        if _has_column(row, f):
            d[f] = _pick(row, f)
    if _has_column(row, "tenant_email"):
        t = split_emails(_pick(row, "tenant_email"))
        d["tenant_email"], d["tenant_additional_emails"] = (t[0] if t else ""), ";".join(t[1:])
    for f in ("tenant_start", "tenant_end"):
        if _has_column(row, f):
            val, ok = parse_date(_pick(row, f))
            d[f] = val
            if not ok:
                notes.append(f"Couldn't read the date '{_pick(row, f)}' for {f.replace('_', ' ')}; it was left blank.")
    if d.get("tenant_name") and d.get("tenant_end") and d["tenant_end"] < date.today():
        notes.append(f"Tenant {d['tenant_name']}'s lease ended {d['tenant_end']:%m/%d/%Y}, so they will not receive notices.")
    if _any_column(row, MAILING_FIELDS):
        # "Mailing Address" is the owner's mailing street line. ("Mailing Address 1" repeats it without the house number, so it is
        # only a fallback.) Vantaca's report has no mailing city/state/ZIP unless those columns are added.
        text = (_pick(row, "mailing_address") or _pick(row, "mailing_address1")).strip().rstrip(".")
        if not text or _norm_street(text) in _norm_street(street) or _norm_street(street) in _norm_street(text):
            d.update(mailing_address="", mail_line1="", mail_city="", mail_state="", mail_zip="")  # same place: no second address
        else:
            parsed = parse_us_address(text)
            d["mailing_address"] = text
            d.update(mail_line1=(parsed or {}).get("line1", text), mail_city=(parsed or {}).get("city", _pick(row, "mail_city")),
                     mail_state=(parsed or {}).get("state", _pick(row, "mail_state")), mail_zip=(parsed or {}).get("zip", _pick(row, "mail_zip")))
            if not (d["mail_city"] and d["mail_state"] and d["mail_zip"]):
                notes.append("The mailing address is somewhere other than the property but has no city, state and ZIP, which the print service "
                             "needs. Letters will go to the property address only until the mailing city, state and ZIP are added.")
    d["_notes"] = notes
    return d


def upsert_owner(row):
    """Returns ('created' | 'updated' | 'skipped', owner_or_None).
    Matched on Homeowner ID, then Account Number, then property address (so hand-entered owners are linked, not duplicated)."""
    d = normalize(row)
    if not d["name"] or not d["property_address"]:
        return "skipped", None
    owner = None
    if d["external_id"]:
        owner = Owner.objects.filter(external_id=d["external_id"]).first()
    if owner is None and d["account_number"]:
        owner = Owner.objects.filter(account_number=d["account_number"]).first()
    if owner is None:
        owner = Owner.objects.filter(property_address__iexact=d["property_address"], active=True).first() \
            or Owner.objects.filter(property_address__iexact=d["property_address"], external_id__isnull=True).first()
        # The address belongs to a different Vantaca owner: the home was sold. Keep the old owner (and their violations) as a
        # former owner and start a fresh record for the new one.
        if owner is not None and owner.external_id and d["external_id"] and owner.external_id != d["external_id"]:
            owner.active, owner.inactive_reason = False, "Home sold: the address now belongs to a different Vantaca owner"
            owner.save()
            accounts.sync_account(owner)
            owner = None
    created = owner is None
    owner = owner or Owner()
    for f in ("name", "property_address", "email", "additional_emails", "phone"):
        setattr(owner, f, d[f])
    for f in ("city", "state", "zip_code", "lot_no", "account_number"):  # keep what we have when the export has no value
        if d[f]:
            setattr(owner, f, d[f])
    for f in ("lat", "lng"):
        if d[f] is not None:
            setattr(owner, f, d[f])
    for f in ("second_owner_name", "co_owner_email", "tenant_name", "tenant_email", "tenant_additional_emails", "tenant_phone",
              "tenant_start", "tenant_end", "mailing_address", "mail_line1", "mail_city", "mail_state", "mail_zip"):
        if f in d:
            setattr(owner, f, d[f])
    if d["external_id"]:
        owner.external_id = d["external_id"]
    owner.last_synced_at = timezone.now()
    owner.active = True  # present in the feed, so currently an owner
    owner.save()
    return ("created" if created else "updated"), owner


class SafetyStop(Exception):
    pass


def sync_rows(rows, invite=False, deactivate_missing=False, force=False):
    """Upsert owners, then keep logins in step:
    invite=True            email new owners a link to set up their login
    deactivate_missing=True owners (with a Vantaca ID) absent from this FULL feed are marked inactive and lose access.
                            Refuses if that would deactivate more than 20% of owners (a bad/partial feed) unless force."""
    result = {"created": 0, "updated": 0, "skipped": 0, "invited": 0, "deactivated": 0, "logins_updated": 0}
    seen = set()
    rows, _ = infer_lots(list(rows))
    for r in rows:
        status, owner = upsert_owner(r)
        result[status] += 1
        if owner:
            seen.add(owner.pk)
            if accounts.sync_account(owner) == "updated":
                result["logins_updated"] += 1
            if invite and accounts.invite_owner(owner):
                result["invited"] += 1
    if deactivate_missing:
        missing = list(Owner.objects.filter(active=True, external_id__isnull=False).exclude(pk__in=seen))
        total = Owner.objects.filter(active=True).count()
        if not seen:
            raise SafetyStop("The feed was empty; refusing to deactivate anyone.")
        if total and len(missing) / total > 0.20 and not force:
            raise SafetyStop(f"{len(missing)} of {total} owners are missing from the feed (over 20%); "
                             "looks like a partial feed. Re-run with --force if that is correct.")
        for o in missing:
            o.active, o.inactive_reason = False, "No longer in the Vantaca feed"
            o.save()
            accounts.sync_account(o)
            result["deactivated"] += 1
    return result


def sync_csv(fileobj_or_text, **kw):
    text = fileobj_or_text if isinstance(fileobj_or_text, str) else fileobj_or_text.read()
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    return sync_rows(csv.DictReader(io.StringIO(text)), **kw)


def api_request(method, path, body=None):
    """Authenticated JSON call to the Vantaca API (HTTP Basic auth). Returns the parsed reply."""
    if not (settings.VANTACA_BASE_URL and settings.VANTACA_USER):
        raise RuntimeError("Vantaca API is not configured (VANTACA_BASE_URL / VANTACA_USER / VANTACA_PASSWORD).")
    url = settings.VANTACA_BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    token = base64.b64encode(f"{settings.VANTACA_USER}:{settings.VANTACA_PASSWORD}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {token}", "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def fetch_owners_from_api():
    if not (settings.VANTACA_BASE_URL and settings.VANTACA_OWNERS_PATH and settings.VANTACA_USER):
        raise RuntimeError("Set VANTACA_BASE_URL, VANTACA_OWNERS_PATH, VANTACA_USER and VANTACA_PASSWORD.")
    url = settings.VANTACA_BASE_URL.rstrip("/") + "/" + settings.VANTACA_OWNERS_PATH.lstrip("/")
    token = base64.b64encode(f"{settings.VANTACA_USER}:{settings.VANTACA_PASSWORD}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if isinstance(data, dict):  # tolerate {"data": [...]} / {"items": [...]} wrappers
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return data


def sync_api(**kw):
    return sync_rows(fetch_owners_from_api(), **kw)


def infer_lots(rows):
    """If the homes are numbered 1..N, exactly ONE row has a blank Lot No, and exactly ONE number from 1..N is unused, that blank
    gets the unused number. Anything less certain is left alone. Returns (rows, notes) where notes is [(row_number, message)]."""
    rows = list(rows)
    if not rows or not any(_has_column(r, "lot_no") for r in rows):
        return rows, []
    lots = [_pick(r, "lot_no") for r in rows]
    blanks = [i for i, v in enumerate(lots) if not v]
    if not blanks or (len(rows) - len(blanks)) / len(rows) < 0.8:  # only when the Lot column is clearly in use (most rows filled)
        return rows, []
    notes = []
    nums = [int(v) for v in lots if v.isdigit()]
    unused = sorted(set(range(1, len(rows) + 1)) - set(nums))
    ok = len(nums) == len(set(nums)) and all(v.isdigit() for v in lots if v)
    if len(blanks) == 1 and len(unused) == 1 and ok:
        i, n = blanks[0], unused[0]
        lot_aliases = {_norm(a) for a in FIELD_ALIASES["lot_no"]}
        key = next((k for k in rows[i] if _norm(k) in lot_aliases), FIELD_ALIASES["lot_no"][0])
        filled = [dict(r) for r in rows]
        filled[i][key] = str(n)
        notes.append((i + 1, f"Lot no. was blank. Set to {n}, the only number missing from lots 1-{len(rows)}."))

        def ends_with_lot(r):  # Vantaca's other addresses end with '#<their lot>'
            lot, addr = _pick(r, "lot_no"), _pick(r, "address1") or _pick(r, "property_address")
            return bool(lot) and addr.rstrip().endswith("#" + lot)
        others = [r for j, r in enumerate(filled) if j != i]
        street = _pick(filled[i], "address1") or _pick(filled[i], "property_address")
        if others and sum(map(ends_with_lot, others)) / len(others) > 0.9 and not street.rstrip().endswith(f"#{n}"):
            notes.append((i + 1, f"Other homes' addresses end with their lot number, but this one has no '#{n}'. The address was left as it "
                                 f"is ('{street}'): check it in Vantaca."))
        return filled, notes
    why = ("more than one Lot No. is blank" if len(blanks) > 1 else "there is more than one unused number" if len(unused) != 1 else
           "the Lot Nos. aren't a clean 1-%d list" % len(rows))
    return rows, [(i + 1, f"Lot no. is blank and couldn't be worked out automatically ({why}).") for i in blanks]


def check_rows(rows):
    """Problems worth fixing before importing: [(row_number, message)]. Row 1 is the first data row."""
    rows, lot_notes = infer_lots(rows)
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError
    problems, seen = [], {}
    for i, row in enumerate(rows, start=1):
        d = normalize(row)
        if not d["name"] or not d["property_address"]:
            problems.append((i, "Skipped: needs an owner name and a property address."))
            continue
        key = d["property_address"].lower()
        if key in seen:
            problems.append((i, f"Same property address as row {seen[key]}: the later row wins."))
        seen[key] = i
        for e in split_emails(";".join(x for x in (d["email"], d["additional_emails"], d.get("co_owner_email", ""), d.get("tenant_email", ""),
                                                   d.get("tenant_additional_emails", "")) if x)):
            try:
                validate_email(e)
            except ValidationError:
                problems.append((i, f"'{e}' doesn't look like a valid email."))
        if not d["zip_code"]:
            problems.append((i, "No ZIP code (needed for mailing and the map)."))
        problems += [(i, n) for n in d["_notes"]]
    return sorted(problems + lot_notes, key=lambda x: x[0])


class UploadError(ValueError):
    """A problem with the uploaded file, worded for the person who uploaded it."""


def rows_from_upload(data: bytes, filename: str = ""):
    """Read an uploaded owner list into a list of {column: value} rows. Accepts Excel (.xlsx) and CSV in any common layout:
    Windows, Mac or Unix line endings, a BOM, comma / semicolon / tab / pipe separators, UTF-8 or Windows-1252 text."""
    if not data.strip():
        raise UploadError("That file is empty.")
    if data[:2] == b"PK":  # a zip container: an .xlsx workbook
        try:
            from openpyxl import load_workbook
            ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        except Exception:
            raise UploadError("That Excel file couldn't be read. Try saving it as CSV and uploading that.")
        grid = [[("" if c is None else (str(int(c)) if isinstance(c, float) and c == int(c) else str(c).strip())) for c in row]
                for row in ws.iter_rows(values_only=True)]
    elif data[:4] == b"\xd0\xcf\x11\xe0":
        raise UploadError("That is an old-style Excel (.xls) file. Open it in Excel and use Save As -> CSV or .xlsx, then upload that.")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")  # Windows, old-Mac and Unix line endings
        if "\x00" in text:
            raise UploadError("That doesn't look like a CSV or Excel file.")
        first = text.split("\n", 1)[0]
        delim = max([",", ";", "\t", "|"], key=first.count)
        try:
            grid = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
        except csv.Error as e:
            raise UploadError(f"That file couldn't be read as a table ({e}).")
    grid = [r for r in grid if any(str(c).strip() for c in r)]
    if len(grid) < 2:
        raise UploadError("The file needs a header row (column names) and at least one owner row.")
    header = [str(h).strip() for h in grid[0]]
    if not any(_norm(h) for h in header):
        raise UploadError("The first row should be the column names, for example Owner Name, Property Address, ZIP.")
    return [{h: (str(c).strip() if c is not None else "") for h, c in zip(header, row) if h} for row in grid[1:]]


def preview_rows(rows):
    """What importing would do, without changing anything."""
    from django.db import transaction
    with transaction.atomic():
        result = sync_rows(rows)
        transaction.set_rollback(True)
    return rows, result, check_rows(rows)


def preview_csv(text):
    """What importing would do, without changing anything."""
    from django.db import transaction
    rows = list(csv.DictReader(io.StringIO(text)))
    with transaction.atomic():
        result = sync_rows(rows)
        transaction.set_rollback(True)
    return rows, result, check_rows(rows)


# ---- column matching for the import page ----
MAPPABLE = [("external_id", "Homeowner ID"), ("account_number", "Account Number"), ("name", "Homeowner name 1 (Homeowner(s))"),
            ("first", "First name"), ("last", "Last name"), ("second_first", "Second owner first name"),
            ("second_last", "Second owner last name"), ("property_address", "Address (physical)"), ("street_no", "Street no"),
            ("address1", "Address 1"), ("lot_no", "Lot no"), ("city", "City"), ("state", "State"), ("zip_code", "ZIP"),
            ("mailing_address", "Mailing address"), ("mailing_address1", "Mailing address 1"), ("mail_city", "Mailing city"),
            ("mail_state", "Mailing state"), ("mail_zip", "Mailing ZIP"), ("email", "Email(s) (several separated by ;)"),
            ("co_owner_email", "Co-owner email"), ("phone", "Phone"), ("tenant_name", "Tenant name(s)"),
            ("tenant_email", "Tenant email(s)"), ("tenant_phone", "Tenant phone(s)"), ("tenant_start", "Tenant start date"),
            ("tenant_end", "Tenant end date"), ("lat", "Latitude"), ("lng", "Longitude")]


def header_signature(headers):
    return "|".join(_norm(h) for h in headers)[:500]


def auto_mapping(headers):
    """Best guess for each column from its name: {header: field or ''}. Each field is claimed by the first column that fits."""
    out, taken = {}, set()
    for h in headers:
        field = next((f for f, _ in MAPPABLE if f not in taken and _norm(h) in {_norm(a) for a in FIELD_ALIASES[f]}), "")
        out[h] = field
        if field:
            taken.add(field)
    return out


def apply_mapping(rows, mapping):
    """Rename each row's columns to the names the importer understands; unmatched columns are dropped."""
    canon = {f: FIELD_ALIASES[f][0] for f, _ in MAPPABLE}
    return [{canon[mapping[h]]: v for h, v in row.items() if mapping.get(h)} for row in rows]


def mapping_problems(mapping):
    fields = [f for f in mapping.values() if f]
    probs = []
    if not ("name" in fields or "first" in fields or "last" in fields):
        probs.append("Match a column to Owner name (or First name and Last name).")
    if "property_address" not in fields:
        probs.append("Match a column to Property address.")
    dup = sorted({dict(MAPPABLE)[f] for f in fields if fields.count(f) > 1})
    if dup:
        probs.append("More than one column is matched to: " + ", ".join(dup) + ". Only one column can be used for each.")
    return probs


def group_problems(problems):
    """[(row, message)] -> [(message, [rows])], in order of first appearance, so one issue affecting 40 rows is one line."""
    groups = {}
    for row, msg in problems:
        groups.setdefault(msg, []).append(row)
    return list(groups.items())
