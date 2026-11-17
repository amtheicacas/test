"""Hand letters to a third-party print-and-mail provider.

MAIL_PROVIDER=manual (default): nothing is sent; staff print the PDF and log mailing by hand.
MAIL_PROVIDER=lob: each notice is printed and mailed automatically via Lob (api.lob.com). Hearing notices go
certified with return receipt AND first-class (per the procedure); other notices go first-class.
The Lob request shape follows their documented /v1/letters API but has not been run against a live account:
use a Lob *test* key first and check the result before switching to a live key.
A provider failure never blocks the workflow: it is logged and the notice shows as "not mailed" on the dashboard.
"""
import base64
import json
import uuid
import urllib.request
from datetime import date

from django.conf import settings

from .models import Notice

K = Notice.Kind


def _services_for(kind):
    if kind == K.HEARING:
        return [("certified_return_receipt", "certified"), (None, "first_class")]
    return [(None, "first_class")]


def _multipart(fields, file_field, filename, file_bytes):
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f'Content-Type: application/pdf\r\n\r\n'.encode() + file_bytes + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def lob_send(to, pdf, description, extra_service=None):
    ret = {"name": settings.HOA_NAME[:40], "line1": settings.HOA_RETURN_LINE1, "city": settings.HOA_RETURN_CITY,
           "state": settings.HOA_RETURN_STATE, "zip": settings.HOA_RETURN_ZIP}
    if not all(to.get(k) for k in ("name", "line1", "city", "state", "zip")):
        raise ValueError("incomplete mailing address (need street, city, state, ZIP)")
    if not all(ret.values()):
        raise ValueError("HOA return address not configured (HOA_RETURN_*)")
    fields = {"description": description[:255], "color": "false", "double_sided": "true",
              "address_placement": "insert_blank_page", "mail_type": "usps_first_class"}
    for pre, a in (("to", to), ("from", ret)):
        fields.update({f"{pre}[name]": a["name"], f"{pre}[address_line1]": a["line1"], f"{pre}[address_city]": a["city"],
                       f"{pre}[address_state]": a["state"], f"{pre}[address_zip]": a["zip"]})
    if extra_service:
        fields["extra_service"] = extra_service
    body, ctype = _multipart(fields, "file", "notice.pdf", pdf)
    token = base64.b64encode(f"{settings.LOB_API_KEY}:".encode()).decode()
    req = urllib.request.Request("https://api.lob.com/v1/letters", data=body, method="POST",
                                 headers={"Authorization": f"Basic {token}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def dispatch(notice, render):
    """Print and mail a notice to the owner (and tenant). `render(recipient)` returns PDF bytes.
    Returns True if it was handed to a provider."""
    from .services import log  # avoid circular import
    v = notice.violation
    if settings.MAIL_PROVIDER != "lob":
        return False
    recipients = [("owner", False)] + ([("tenant", True)] if v.owner.tenant_name else [])
    ids, tracking, errors = [], "", []
    for who, is_tenant in recipients:
        pdf = render(who)
        for extra, label in _services_for(notice.kind):
            try:
                r = lob_send(v.owner.postal(tenant=is_tenant), pdf, f"Case {v.pk} {notice.kind} ({who}, {label})", extra)
                ids.append(r.get("id", "?"))
                tracking = tracking or r.get("tracking_number") or ""
            except Exception as e:  # never block the workflow on the provider
                errors.append(f"{who}/{label}: {e}")
    notice.provider, notice.provider_ids = "lob", ",".join(ids)[:500]
    notice.provider_note = "; ".join(errors)[:300]
    if ids:
        notice.mailed_on = date.today()
        notice.tracking_number = tracking
        notice.method = "certified" if notice.kind == K.HEARING else "first_class"
    notice.save()
    log(v, f"{notice.get_kind_display()} sent to print/mail provider ({len(ids)} piece(s))"
        + (f" — ERRORS: {notice.provider_note}" if errors else ""))
    return bool(ids)
