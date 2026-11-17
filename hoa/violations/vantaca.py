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
}


def _norm(k):
    return "".join(c for c in str(k).lower() if c.isalnum())


def _pick(row, field):
    lookup = {_norm(k): v for k, v in row.items()}
    for alias in FIELD_ALIASES[field]:
        v = lookup.get(alias)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def normalize(row):
    name = _pick(row, "name") or f"{_pick(row, 'first')} {_pick(row, 'last')}".strip()
    return {"external_id": _pick(row, "external_id") or None, "name": name,
            "property_address": _pick(row, "property_address"), "email": _pick(row, "email"),
            "phone": _pick(row, "phone"), "mailing_address": _pick(row, "mailing_address"),
            **{f: _pick(row, f) for f in ("city", "state", "zip_code", "mail_line1", "mail_city", "mail_state", "mail_zip")},
            **{f: (float(_pick(row, f)) if _pick(row, f) else None) for f in ("lat", "lng")}}


def upsert_owner(row):
    """Returns 'created' | 'updated' | 'skipped'."""
    d = normalize(row)
    if not d["name"] or not d["property_address"]:
        return "skipped"
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
    if d["external_id"]:
        owner.external_id = d["external_id"]
    owner.last_synced_at = timezone.now()
    owner.save()
    return "created" if created else "updated"


def sync_rows(rows):
    result = {"created": 0, "updated": 0, "skipped": 0}
    for r in rows:
        result[upsert_owner(r)] += 1
    return result


def sync_csv(fileobj_or_text):
    text = fileobj_or_text if isinstance(fileobj_or_text, str) else fileobj_or_text.read()
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    return sync_rows(csv.DictReader(io.StringIO(text)))


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


def sync_api():
    return sync_rows(fetch_owners_from_api())
