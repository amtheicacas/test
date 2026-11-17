"""Put homes on the map using the US Census Bureau's free batch geocoder (no key needed).
Not run against the live service from the build environment: check the first batch's results."""
import csv
import io
import urllib.request
import uuid

from .models import Owner

BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


def _post(rows):
    """rows: [(id, street, city, state, zip)] -> raw CSV text from the Census geocoder."""
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="benchmark"\r\n\r\nPublic_AR_Current\r\n'.encode(),
             f'--{boundary}\r\nContent-Disposition: form-data; name="addressFile"; filename="a.csv"\r\n'
             f'Content-Type: text/csv\r\n\r\n'.encode() + buf.getvalue().encode() + b"\r\n", f"--{boundary}--\r\n".encode()]
    req = urllib.request.Request(BATCH_URL, data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(text):
    """Census reply -> {id: (lat, lng)} for matched rows. Columns: id, input, Match|No_Match|Tie, type, matched, "lon,lat", ..."""
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 6 and row[2].strip() == "Match" and "," in row[5]:
            lon, lat = row[5].split(",")[:2]
            out[row[0]] = (float(lat), float(lon))
    return out


def geocode_missing(poster=None, chunk=1000):
    """Locate every active owner without coordinates. Returns (located, not_found)."""
    poster = poster or _post
    owners = list(Owner.objects.filter(lat__isnull=True, active=True))
    located = 0
    for i in range(0, len(owners), chunk):
        batch = owners[i:i + chunk]
        found = parse(poster([(o.pk, o.property_address.split("#")[0].strip(), o.city, o.state, o.zip_code) for o in batch]))
        for o in batch:
            if str(o.pk) in found:
                o.lat, o.lng = found[str(o.pk)]
                o.save(update_fields=["lat", "lng"])
                located += 1
    return located, len(owners) - located
