import json
import urllib.parse
import urllib.request

from django.core.management.base import BaseCommand

from violations.models import Owner

URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def geocode(address):
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    with urllib.request.urlopen(f"{URL}?{q}", timeout=30) as r:
        matches = json.load(r)["result"]["addressMatches"]
    if not matches:
        return None
    c = matches[0]["coordinates"]
    return c["y"], c["x"]


class Command(BaseCommand):
    help = "Look up map coordinates (US Census geocoder, free, no key) for owners that have none."

    def handle(self, *a, **kw):
        ok = bad = 0
        for o in Owner.objects.filter(lat__isnull=True):
            full = f"{o.property_address}, {o.city}, {o.state} {o.zip_code}".strip()
            try:
                hit = geocode(full)
            except Exception as e:
                self.stderr.write(f"{full}: {e}")
                hit = None
            if hit:
                o.lat, o.lng = hit
                o.save(update_fields=["lat", "lng"])
                ok += 1
            else:
                bad += 1
        self.stdout.write(f"located {ok}, not found {bad}")
