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
import urllib.request

from django.conf import settings
from django.utils import timezone

from . import accounts
from .models import Owner

# our field -> possible column / JSON keys (case-insensitive, spaces/underscores ignored)
FIELD_ALIASES = {
    "external_id": ["accountid", "accountnumber", "homeownerid", "ownerid", "id"],
    "name": ["name", "ownername", "homeownername", "fullname", "homeowner"],
    "first": ["firstname"], "last": ["lastname"],
    "property_address": ["propertyaddress", "address", "unitaddress", "streetaddress", "property"],
    "email": ["email", "emailaddress", "primaryemail"],
    "phone": ["phone", "phonenumber", "primaryphone", "homephone", "cellphone"],
    "mailing_address": ["mailingaddress", "mailaddress", "billingaddress"],
    "city": ["city", "propertycity"], "state": ["state", "propertystate"], "zip_code": ["zip", "zipcode", "postalcode", "propertyzip"],
    "mail_line1": ["mailingstreet", "mailingaddress1", "mailaddress1"], "mail_city": ["mailingcity"],
    "mail_state": ["mailingstate"], "mail_zip": ["mailingzip", "mailingzipcode"],
    "lat": ["latitude", "lat"], "lng": ["longitude", "lng", "lon"],
    "co_owner_email": ["coowneremail", "coowneremailaddress", "secondaryemail", "email2", "secondownersemail"],
    "tenant_name": ["tenantname", "renter", "renterName", "tenant", "occupantname"],
    "tenant_email": ["tenantemail", "renteremail", "occupantemail"],
    "tenant_phone": ["tenantphone", "renterphone"],
}
HOUSEHOLD_FIELDS = ("co_owner_email", "tenant_name", "tenant_email", "tenant_phone")


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


def normalize(row):
    name = _pick(row, "name") or f"{_pick(row, 'first')} {_pick(row, 'last')}".strip()
    return {"external_id": _pick(row, "external_id") or None, "name": name,
            "property_address": _pick(row, "property_address"), "email": _pick(row, "email"),
            "phone": _pick(row, "phone"), "mailing_address": _pick(row, "mailing_address"),
            **{f: _pick(row, f) for f in ("city", "state", "zip_code", "mail_line1", "mail_city", "mail_state", "mail_zip")},
            **{f: (float(_pick(row, f)) if _pick(row, f) else None) for f in ("lat", "lng")},
            # tenant / co-owner: only touched when the file has that column (a blank then means "none", e.g. the tenant moved out)
            **{f: _pick(row, f) for f in HOUSEHOLD_FIELDS if _has_column(row, f)}}


def upsert_owner(row):
    """Returns ('created' | 'updated' | 'skipped', owner_or_None)."""
    d = normalize(row)
    if not d["name"] or not d["property_address"]:
        return "skipped", None
    owner = None
    if d["external_id"]:
        owner = Owner.objects.filter(external_id=d["external_id"]).first()
    if owner is None:
        owner = Owner.objects.filter(property_address__iexact=d["property_address"]).first()
    created = owner is None
    owner = owner or Owner()
    for f in ("name", "property_address", "email", "phone", "mailing_address", "mail_line1", "mail_city",
              "mail_state", "mail_zip"):
        setattr(owner, f, d[f])
    for f in ("city", "state", "zip_code"):  # keep model defaults (Sarasota, FL) when the export has no value
        if d[f]:
            setattr(owner, f, d[f])
    for f in ("lat", "lng"):
        if d[f] is not None:
            setattr(owner, f, d[f])
    for f in HOUSEHOLD_FIELDS:
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
            o.active = False
            o.save(update_fields=["active"])
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


def check_rows(rows):
    """Problems worth fixing before importing: [(row_number, message)]. Row 1 is the first data row."""
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
        for f in ("email", "co_owner_email", "tenant_email"):
            if d.get(f):
                try:
                    validate_email(d[f])
                except ValidationError:
                    problems.append((i, f"'{d[f]}' doesn't look like a valid email."))
        if not d["zip_code"]:
            problems.append((i, "No ZIP code (needed for mailing and the map)."))
    return problems


def preview_csv(text):
    """What importing would do, without changing anything."""
    from django.db import transaction
    rows = list(csv.DictReader(io.StringIO(text)))
    with transaction.atomic():
        result = sync_rows(rows)
        transaction.set_rollback(True)
    return rows, result, check_rows(rows)
