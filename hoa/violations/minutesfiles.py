"""Uploaded committee minutes: only PDF and Word (.docx), decided from what the file really is, stored under a random name, add-only.

A new upload never touches the earlier one: it adds a `MinutesFile` that points at the file it replaced, so the history stays complete.
Files are only ever sent through `serve` (a permission-checked view): always as a download, with a fixed type, never as a page."""
import hashlib
import io
import re
import zipfile

from django.http import FileResponse
from django.utils import timezone

MAX_BYTES = 10 * 1024 * 1024
MAX_UNZIPPED = 100 * 1024 * 1024      # a Word file is a zip: refuse one that would unpack to something absurd (a "zip bomb")
MAX_PARTS = 3000
CONTENT_TYPES = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
REFUSED = "Upload the minutes as a PDF or a Word (.docx) document under 10 MB. Older Word (.doc) files, text files and pictures are not accepted: save as PDF or .docx first."


def _pdf_ok(data):
    from pypdf import PdfReader
    if not data[:1024].lstrip(b"\x00\t\r\n ").startswith(b"%PDF-"):
        return False
    if b"/JavaScript" in data or b"/Launch" in data:           # minutes never need a script or a program launcher
        return False
    try:
        return len(PdfReader(io.BytesIO(data)).pages) > 0
    except Exception:       # noqa: BLE001  (damaged or not really a PDF)
        return False


def _docx_ok(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
            if len(infos) > MAX_PARTS or sum(i.file_size for i in infos) > MAX_UNZIPPED:
                return False
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return False
            if any("vbaproject" in n.lower() or n.lower().startswith("word/embeddings/") for n in names):     # macros / embedded programs
                return False
            return z.testzip() is None
    except Exception:       # noqa: BLE001
        return False


def kind_of(data):
    """'pdf' or 'docx' if the bytes really are one, else None. The file name is never consulted."""
    if _pdf_ok(data):
        return "pdf"
    if data[:2] == b"PK" and _docx_ok(data):
        return "docx"
    return None


def clean_name(name):
    """The uploader's file name, made safe to show: no folders, no control characters, at most 120 characters."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:120] or "minutes"


def current(meeting):
    return meeting.minutes_files.first()


def add(meeting, upload, user):
    """Validate and store an uploaded file. Returns (MinutesFile or None, error text or '')."""
    from django.core.files.base import ContentFile
    from .models import MinutesFile
    if upload.size > MAX_BYTES or upload.size == 0:
        return None, REFUSED
    data = upload.read()
    kind = kind_of(data)
    if not kind:
        return None, REFUSED
    row = MinutesFile(meeting=meeting, kind=kind, sha256=hashlib.sha256(data).hexdigest(), size=len(data), original_name=clean_name(upload.name),
                      uploaded_by=user if getattr(user, "pk", None) else None,
                      uploaded_by_name=(user.get_full_name() or user.get_username()) if getattr(user, "pk", None) else "",
                      replaces=current(meeting))
    row.file.save(f"minutes.{kind}", ContentFile(data), save=False)       # saved under a random name
    row.save()
    return row, ""


def serve(meeting, fieldfile, kind, data=None, version=""):
    """Always an attachment with a fixed type (never opened as a page), never cached, never sniffed."""
    ctype = CONTENT_TYPES.get(kind)
    day = f"{timezone.localtime(meeting.starts_at):%Y-%m-%d}"
    name = f"committee-minutes-{day}{('-' + str(version)) if version else ''}.{kind if ctype else 'bin'}"
    resp = FileResponse(io.BytesIO(data) if data is not None else fieldfile.open("rb"), content_type=ctype or "application/octet-stream",
                        as_attachment=True, filename=name)
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp
