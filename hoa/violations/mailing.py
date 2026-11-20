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

from django.utils import timezone

from .models import MailPiece, Notice

K = Notice.Kind


def _services_for(kind):
    """Per the Fining Resolution: the Notice of Hearing goes certified mail, return receipt requested, with an optional extra
    regular copy; the Notice of Imposition carries a certified mail number too. Everything else is first-class."""
    if kind == K.HEARING:
        return [("certified_return_receipt", "certified")] + ([(None, "first_class")] if settings.HEARING_REGULAR_COPY else [])
    if kind == K.FINE:
        return [("certified_return_receipt", "certified")]
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


def lob_get(letter_id):
    """Fetch one letter from Lob, including its USPS tracking events (certified mail)."""
    token = base64.b64encode(f"{settings.LOB_API_KEY}:".encode()).decode()
    req = urllib.request.Request(f"https://api.lob.com/v1/letters/{letter_id}", headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


PROBLEM_WORDS = ("returned", "re-routed", "rerouted", "undeliverable", "return to sender", "notice left", "pick up", "no such", "refused", "vacant")


def apply_tracking(piece, r, mark_checked=True):
    """Copy a Lob letter response onto the piece: tracking number, expected delivery, events, latest status, delivered date, problems.
    Returns True if anything changed."""
    before = (piece.tracking_number, piece.status, piece.delivered_on, piece.problem, len(piece.events or []))
    piece.tracking_number = r.get("tracking_number") or piece.tracking_number
    exp = r.get("expected_delivery_date")
    if exp:
        try:
            piece.expected_delivery_on = date.fromisoformat(str(exp)[:10])
        except ValueError:
            pass
    events = []
    for e in r.get("tracking_events") or []:
        events.append({"name": e.get("name") or "", "location": e.get("location") or "", "time": e.get("time") or e.get("date_created") or ""})
    events.sort(key=lambda e: e["time"])
    if events:
        piece.events = events
        piece.status = events[-1]["name"]
        for e in events:
            if e["name"].lower().startswith("delivered") and not piece.delivered_on:
                try:
                    piece.delivered_on = date.fromisoformat(e["time"][:10])
                except ValueError:
                    piece.delivered_on = date.today()
        piece.problem = any(w in e["name"].lower() for e in events for w in PROBLEM_WORDS) and not piece.delivered_on
    if mark_checked:
        piece.checked_at = timezone.now()
    piece.save()
    n = piece.notice
    if piece.is_certified:
        changed = False
        if piece.tracking_number and not n.tracking_number:
            n.tracking_number, changed = piece.tracking_number, True
        if piece.delivered_on and not n.return_receipt_on:
            n.return_receipt_on, changed = piece.delivered_on, True
        if changed:
            n.save()
    return before != (piece.tracking_number, piece.status, piece.delivered_on, piece.problem, len(piece.events or []))


def refresh_piece(piece):
    """Ask the provider for the latest tracking on one piece. Never raises: a provider hiccup just leaves the old status."""
    if settings.MAIL_PROVIDER != "lob" or not piece.provider_id:
        return False
    try:
        return apply_tracking(piece, lob_get(piece.provider_id))
    except Exception:
        return False


def dispatch(notice, render, user=None):
    """Print and mail a notice. The owner's letter goes to the mailing address (if there is one) AND the property address;
    while the lease is active the tenant's goes to the property. `render(variant)` returns PDF bytes for 'owner' (mailing address),
    'owner_property' or 'tenant'. Returns True if anything was handed to a provider."""
    from .services import log  # avoid circular import
    v = notice.violation
    if settings.MAIL_PROVIDER != "lob":
        return False
    o = v.owner
    targets = [("owner" if k == "mailing" else "owner_property", o.postal(k), f"owner {k}") for k in o.owner_mail_kinds()]
    if o.has_tenant:
        targets.append(("tenant", o.postal("tenant"), "tenant"))
    ids, tracking, errors = [], "", []
    for variant, addr, label in targets:
        if not all(addr.get(k) for k in ("name", "line1", "city", "state", "zip")):  # skip this address, keep mailing the others
            errors.append(f"{label}: incomplete mailing address (need street, city, state, ZIP)")
            continue
        pdf = render(variant)
        for extra, svc in _services_for(notice.kind):
            try:
                r = lob_send(addr, pdf, f"Case {v.pk} {notice.kind} ({label}, {svc})", extra)
                ids.append(r.get("id", "?"))
                if svc == "certified":
                    tracking = tracking or r.get("tracking_number") or ""
                piece = MailPiece.objects.create(notice=notice, provider_id=r.get("id", ""), label=label, service=svc,
                                                 tracking_number=r.get("tracking_number") or "")
                apply_tracking(piece, r, mark_checked=False)   # the send response is not a tracking check
                from .services import _keep_copy
                _keep_copy(notice, pdf, variant, "certified" if svc == "certified" else "first_class",
                           f"{addr['name']}, {addr['line1']}, {addr['city']}, {addr['state']} {addr['zip']}", addr["name"], user, errors,
                           tracking=piece.tracking_number, piece=piece)
            except Exception as e:  # never block the workflow on the provider
                errors.append(f"{label}/{svc}: {e}")
    notice.provider, notice.provider_ids = "lob", ",".join(ids)[:500]
    notice.provider_note = "; ".join(errors)[:300]
    if ids:
        notice.mailed_on = date.today()
        notice.tracking_number = tracking
        notice.method = "certified" if notice.kind in (K.HEARING, K.FINE) else "first_class"
    notice.save()
    log(v, f"{notice.get_kind_display()} sent to print/mail provider ({len(ids)} piece(s))"
        + (f" - ERRORS: {notice.provider_note}" if errors else ""))
    return bool(ids)
