"""Safe handling of uploaded photos: the stored type comes from what the file really is, never from the name the uploader typed."""
from django.http import FileResponse

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


def photo_response(photo):
    """Send a stored photo so a browser can only ever treat it as a picture: fixed image type, never a page, and a policy that
    forbids running anything even if a file claimed otherwise. Anything not recognisably an image is offered as a download."""
    ext = photo.image.name.rsplit(".", 1)[-1].lower() if "." in photo.image.name else ""
    ctype = CONTENT_TYPES.get(ext)
    resp = FileResponse(photo.image.open("rb"), content_type=ctype or "application/octet-stream", as_attachment=not ctype,
                        filename=f"photo-{photo.pk}.{ext if ctype else 'bin'}")
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, max-age=300"       # never in a shared cache; a few minutes in the browser keeps list pages quick
    return resp
