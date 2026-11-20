"""Check the private file storage (the bucket) end to end, with a harmless test file. Run it on the PC or on staging after the keys are in
local.env / the host's environment:  python manage.py check_storage

It writes a small file, reads it back and compares fingerprints, proves a link without its signature is refused, proves a second
save never overwrites the first, and reports what the key does when asked to delete. It sends nothing to anyone and touches no case data."""
import hashlib
import secrets
import urllib.error
import urllib.request

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


def fetch_status(url):
    """HTTP status of a plain GET (no cookies, no login)."""
    try:
        with urllib.request.urlopen(url, timeout=20) as r:       # noqa: S310  (our own bucket address)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def run_checks(storage, bucket_mode, fetch=fetch_status):
    """Returns a list of (ok, text). ok is True / False / None (information only)."""
    out = []
    body = b"storage check " + secrets.token_hex(16).encode()
    want = hashlib.sha256(body).hexdigest()
    name = storage.save(f"storage-check/{secrets.token_hex(8)}.txt", ContentFile(body))
    out.append((True, f"wrote {name}"))
    with storage.open(name, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    out.append((got == want, "read it back and the fingerprint matches" if got == want else "READ BACK A DIFFERENT FILE"))
    again = storage.save(name, ContentFile(b"someone else's bytes"))
    with storage.open(name, "rb") as f:
        kept = hashlib.sha256(f.read()).hexdigest() == want
    out.append((kept and again != name, "saving the same name again did not overwrite the first file" if kept and again != name
                else "A SECOND SAVE OVERWROTE THE FIRST FILE"))
    if bucket_mode:
        url = storage.url(name)
        signed = fetch(url)
        out.append((signed == 200, f"the signed link works (HTTP {signed})" if signed == 200 else f"the signed link did NOT work (HTTP {signed})"))
        bare = fetch(url.split("?", 1)[0])
        out.append((bare in (401, 403), f"the same link without its signature is refused (HTTP {bare})" if bare in (401, 403)
                    else f"THE BUCKET ANSWERED A LINK WITHOUT A SIGNATURE (HTTP {bare}): it may be public. Make it private."))
    try:
        storage.delete(name)
    except Exception as e:      # noqa: BLE001
        out.append((True, f"the key was refused when asked to delete ({type(e).__name__}): good for the live key"))
    else:
        try:
            with storage.open(name, "rb") as f:
                f.read()
            out.append((None, "the delete request was accepted but the file is still readable"))
        except Exception:       # noqa: BLE001
            out.append((None, "the delete request was accepted. If versioning is on, the file is only hidden and the earlier version is still stored "
                              "(check 'show all versions' in the bucket); a key that can delete permanently is NOT what we want for the live site"))
    return out


class Command(BaseCommand):
    help = "Write, read back, fingerprint-check and delete-test a small file in the configured file storage."

    def handle(self, *a, **o):
        bucket = bool(getattr(settings, "STORAGE_BUCKET", "") or settings.STORAGES["default"]["BACKEND"].endswith("S3Storage"))
        if not bucket:
            self.stdout.write("No bucket is configured (STORAGE_BUCKET is empty), so this checks the local media folder only.")
        if bucket:
            o_ = settings.STORAGES["default"]["OPTIONS"]       # only what is safe to show: never the key itself
            self.stdout.write(f"Using bucket '{o_.get('bucket_name')}' at {o_.get('endpoint_url') or '(default AWS address)'} "
                              f"(region {o_.get('region_name') or 'not set'}); key ID is {len(o_.get('access_key') or '')} characters, "
                              f"key is {len(o_.get('secret_key') or '')} characters.")
        try:
            results = run_checks(default_storage, bucket)
        except Exception as e:      # noqa: BLE001
            hint = ""
            if "403" in str(e) or "Forbidden" in str(e) or "AccessDenied" in str(e):
                hint = ("\nA 403 means the bucket refused this key. Usual causes: the key was made for a different bucket than the one named above, "
                        "a character was lost or added when the key or its ID was pasted (compare the lengths above with what Backblaze shows), "
                        "the key lacks a permission (it needs listBuckets, listFiles, readFiles, writeFiles), or the bucket is in a different region "
                        "from the endpoint above.")
            raise CommandError(f"The storage did not work: {type(e).__name__}: {e}{hint}")
        bad = 0
        for ok, text in results:
            self.stdout.write(("  OK   " if ok else "  NOTE " if ok is None else "  FAIL ") + text)
            bad += ok is False
        if bad:
            raise CommandError(f"{bad} check(s) failed.")
        self.stdout.write("All checks passed. (The test file is named storage-check/... and can be ignored; with a no-delete key it stays.)")
