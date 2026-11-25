"""Safe handling of uploaded photos: the stored type comes from what the file really is, never from the name the uploader typed."""
import io
import logging

from django.http import FileResponse, Http404

# what Pillow reports -> the extension we store
FORMATS = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}   # MPO = the multi-picture JPEG some phones make
CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def image_ext(f):
    """'jpg' / 'png' / 'gif' / 'webp' if the uploaded file really is such a picture, else None. Leaves the file rewound."""
    from PIL import Image
    try:
        im = Image.open(f)
        ext = FORMATS.get(im.format)
        if ext:
            im.verify()
        return ext
    except Exception:       # noqa: BLE001  (not an image, truncated, or a decompression bomb)
        return None
    finally:
        f.seek(0)


def safe_ext(filename):
    """The extension to store under: one of the picture types above, else 'jpg' (the name is never trusted)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in CONTENT_TYPES else "jpg"


MAX_UPLOAD = 10 * 1024 * 1024         # one picture
MAX_PHOTOS_PER_UPLOAD = 10            # pictures in one submission
MAX_UPLOAD_TOTAL = 40 * 1024 * 1024   # ...and their combined size


def taken_at(f):
    """When the picture was taken, from its own EXIF date (DateTimeOriginal), or None. Leaves the file rewound."""
    from datetime import datetime
    from django.utils import timezone
    from PIL import Image
    try:
        exif = Image.open(f).getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        return timezone.make_aware(datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")) if raw else None
    except Exception:       # noqa: BLE001
        return None
    finally:
        f.seek(0)


def photo_response(photo):
    """Send a stored photo so a browser can only ever treat it as a picture: fixed image type, never a page, and a policy that
    forbids running anything even if a file claimed otherwise. Anything not recognisably an image is offered as a download."""
    return file_response(photo.image, f"photo-{photo.pk}")


def file_response(fieldfile, stem, private=False, data=None):
    """Send a stored picture (see photo_response). private=True: never cached anywhere (internal evidence). `data` = bytes already read."""
    name = fieldfile.name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ctype = CONTENT_TYPES.get(ext)
    kw = dict(content_type=ctype or "application/octet-stream", as_attachment=not ctype, filename=f"{stem}.{ext if ctype else 'bin'}")
    try:
        stream = io.BytesIO(data) if data is not None else fieldfile.open("rb")
    except FileNotFoundError:        # the row exists but the file is gone from storage (lost, moved, a restore without the media files)
        logging.getLogger(__name__).warning("a stored file is missing from storage: %s", name)
        raise Http404 from None
    resp = FileResponse(stream, **kw)
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store" if private else "private, max-age=300"   # never in a shared cache; a few minutes keeps list pages quick
    return resp
