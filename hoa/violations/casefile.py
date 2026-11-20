"""The case file: one zip with everything the application holds about a violation, for the Board, the manager or the attorney."""
import csv
import hashlib
import io
import re
import zipfile
from html import escape

from django.conf import settings
from django.utils import timezone



def _slug(text, n=40):
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")[:n] or "case"


def _when(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else ""


def _day(d):
    return d.isoformat() if d else ""


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")           # the BOM lets Excel on Windows read accents and symbols correctly


def _who(user):
    return (user.get_full_name() or user.get_username()) if user else "the application (automatic)"


def build_case_file(v, user, site_visits=True):
    """Returns (zip bytes, file name). Everything about case `v`: the saved exact copy of every letter, the emails, the mailing and certified
    tracking history, the photos, the hearing and decision records, the change history, the owner messages, and an index of it all."""
    root = f"case-{v.pk}-{_slug(v.owner.property_address)}"
    files = {}              # path inside the zip -> bytes
    index = []              # (section, path, date, what)

    def add(section, path, data, when, what):
        files[f"{root}/{path}"] = data
        index.append((section, path, when, what))

    # ---- case summary -------------------------------------------------------------------------------------------------------------
    o = v.owner
    lines = [f"CASE #{v.pk}: {v.vtype.name}", f"Status: {v.get_status_display()}", "",
             f"Owner: {o.display_name}", f"Property: {o.property_address}",
             f"Owner emails on file: {', '.join(o.all_emails) or '(none)'}",
             f"Tenant: {o.tenant_name if o.has_tenant else '(none)'}", "",
             f"Rule: {v.vtype.reference or v.vtype.standards_title or v.vtype.name}", f"Observed on: {_day(v.observed_on)}",
             f"What was observed: {v.description}", f"What is needed to cure: {v.cure_required}",
             f"Cure period: {v.cure_days} days (+{v.extended_days} extended); deadline {_day(v.cure_deadline) or '(not set)'}",
             f"Case opened: {_when(v.created_at)}" + (f" by {_who(v.created_by)}" if v.created_by else ""),
             f"Corrected on: {_day(v.cured_on) or '(not recorded)'}"]
    add("Case", "01-case-summary.txt", "\n".join(lines).encode("utf-8"), _when(v.created_at), "The case at a glance")

    # ---- letters: the exact copies that were sent -----------------------------------------------------------------------------------
    manifest, missing_copy = [], []
    for s in v.sent_letters.select_related("sent_by"):
        try:
            data = s.read_bytes()
        except Exception as e:      # noqa: BLE001
            index.append(("Letters", "(file could not be read)", _when(s.sent_at), f"{s.get_kind_display()} to {s.recipient_name}: {e}"))
            continue
        ok = hashlib.sha256(data).hexdigest() == s.sha256
        name = f"letters/{s.sent_at:%Y-%m-%d}-{s.kind}-{s.recipient}-{s.channel}-{s.pk}.pdf"
        add("Letters (exact copies as sent)", name, data, _when(s.sent_at),
            f"{s.get_kind_display()} to {s.recipient_name or s.recipient} by {s.get_channel_display()}"
            + (f", tracking {s.tracking_number}" if s.tracking_number else "") + f" - fingerprint {'matches' if ok else 'DOES NOT MATCH'}")
        manifest.append([_when(s.sent_at), s.get_kind_display(), s.recipient, s.recipient_name, s.sent_to, s.get_channel_display(),
                         s.tracking_number, _who(s.sent_by), s.get_origin_display(), s.sha256, "yes" if ok else "NO", name])
    files[f"{root}/letters/letters-manifest.csv"] = _csv(
        ["Sent", "Letter", "Version", "Addressed to", "Sent to", "How", "Tracking number", "Sent by", "Origin", "SHA-256 fingerprint",
         "File matches fingerprint", "File"], manifest)
    index.append(("Letters (exact copies as sent)", "letters/letters-manifest.csv", "", "List of the saved letters with their fingerprints"))
    for n in v.notices.all():
        if not n.sent_copies.exists():
            missing_copy.append(f"{n.get_kind_display()} created {_when(n.created_at)} (no exact copy was saved)")

    # ---- emails ---------------------------------------------------------------------------------------------------------------------
    emails = list(v.emails.all())
    files[f"{root}/emails/emails.csv"] = _csv(["Sent", "To", "Version", "Subject", "Delivered", "Error", "Letter"],
        [[_when(e.sent_at), e.to, e.recipient, e.subject, "yes" if e.ok else "no", e.error,
          e.notice.get_kind_display() if e.notice else ""] for e in emails])
    index.append(("Emails", "emails/emails.csv", "", f"{len(emails)} email(s) sent about this case"))
    for e in emails:
        add("Emails", f"emails/{e.sent_at:%Y-%m-%d}-{e.pk}.txt",
            f"To: {e.to}\nSent: {_when(e.sent_at)}\nSubject: {e.subject}\nDelivered: {'yes' if e.ok else 'no - ' + e.error}\n\n{e.body}".encode("utf-8"),
            _when(e.sent_at), f"Email to {e.to}: {e.subject}")

    # ---- mailing and certified-mail tracking ----------------------------------------------------------------------------------------
    rows, events = [], []
    summary = []
    for n in v.notices.prefetch_related("pieces"):
        summary.append(f"{n.get_kind_display()} (created {_when(n.created_at)}):")
        summary += [f"  - {line}" for line in n.delivery_lines(staff=True)] or ["  - (no delivery recorded)"]
        for p in n.pieces.all():
            rows.append([n.get_kind_display(), p.label, p.get_service_display() if hasattr(p, "get_service_display") else p.service, p.provider_id,
                         p.tracking_number, p.status, _day(p.expected_delivery_on), _day(p.delivered_on), "yes" if p.problem else "",
                         _when(p.checked_at)])
            events += [[n.get_kind_display(), p.tracking_number, e.get("time", ""), e.get("name", ""), e.get("location", "")] for e in p.events or []]
    add("Mailing", "mailing/delivery-summary.txt", "\n".join(summary).encode("utf-8"), "", "How each letter was delivered")
    files[f"{root}/mailing/pieces-and-tracking.csv"] = _csv(
        ["Letter", "Addressed to", "Service", "Provider id", "Tracking number", "Latest status", "Expected", "Delivered", "Problem", "Last checked"], rows)
    files[f"{root}/mailing/tracking-events.csv"] = _csv(["Letter", "Tracking number", "Time", "Event", "Location"], events)
    index += [("Mailing", "mailing/pieces-and-tracking.csv", "", f"{len(rows)} mailed piece(s)"),
              ("Mailing", "mailing/tracking-events.csv", "", f"{len(events)} tracking event(s)")]

    # ---- photos ---------------------------------------------------------------------------------------------------------------------
    prow = []
    for i, p in enumerate(v.photos.order_by("pk"), 1):
        ext = p.image.name.rsplit(".", 1)[-1].lower() if "." in p.image.name else "jpg"
        path = f"photos/photo-{i:02d}-{p.uploaded_at:%Y%m%d}{'-from-owner' if p.from_resident else ''}.{ext}"
        try:
            with p.image.open("rb") as fh:
                add("Photos", path, fh.read(), _when(p.shown_at), ("Photo from the owner" if p.from_resident else "Photo of the condition")
                    + (f" (taken {_when(p.taken_at)})" if p.taken_at else f" (added {_when(p.uploaded_at)})"))
            prow.append([path, _when(p.uploaded_at), _when(p.taken_at), "owner" if p.from_resident else "staff", p.caption])
        except Exception as e:      # noqa: BLE001
            index.append(("Photos", "(could not be read)", _when(p.uploaded_at), str(e)))
    files[f"{root}/photos/photos.csv"] = _csv(["File", "Added", "Taken (from the picture)", "From", "Caption"], prow)
    index.append(("Photos", "photos/photos.csv", "", f"{len(prow)} photo(s)"))

    # ---- hearing and decision -------------------------------------------------------------------------------------------------------
    m = v.committee_meeting
    h = ["BOARD", f"Board meeting date: {_day(v.board_meeting_date) or '(none)'}",
         f"Board vote: {v.board_votes_for if v.board_votes_for is not None else '-'} for, {v.board_votes_against if v.board_votes_against is not None else '-'} against",
         f"Proposed fine: {('$%s' % v.proposed_fine) if v.proposed_fine else '(none)'}"]
    if settings.SUSPENSION_ENABLED:
        h.append(f"Proposed suspension: {v.proposed_suspension or '(none)'}" + (f" for {v.proposed_suspension_days} days" if v.proposed_suspension_days else ""))
    h += ["", "HEARING", f"Hearing: {_when(v.hearing_date) or '(not scheduled)'} {v.hearing_location}".rstrip()]
    if m:
        h += [f"Committee meeting: {_when(m.starts_at)}" + (f", {m.place}" if getattr(m, "place", "") else ""),
              f"Zoom: {m.zoom_link or '(none)'}", "Attendance at that meeting: " + (
                  "; ".join(f"{a.member.name}: {a.status}" for a in m.attendance.select_related("member")) or "(not recorded)")]
    h += ["", "COMMITTEE DECISION",
          f"Decision: {v.get_committee_outcome_display() or '(none recorded)'}"
          + (f" (amended fine ${v.committee_amended_fine})" if v.committee_amended_fine else ""),
          f"Votes: {v.committee_votes_for if v.committee_votes_for is not None else '-'} for, {v.committee_votes_against if v.committee_votes_against is not None else '-'} against",
          f"Owner present at the hearing: {'yes' if v.committee_owner_present else 'no'}",
          "Members who did not vote: " + ("; ".join(f"{a.member.name} ({a.reason})" for a in v.abstentions.select_related("member")) or "(none)"),
          "Findings:", v.committee_findings or "(none recorded)", "",
          "BOARD REVIEW AND IMPOSITION", f"Board review: {v.get_board_outcome_display() or '(none)'} on {_day(v.board_reviewed_on) or '-'}",
          f"Review note: {v.board_review_note or '(none)'}", f"Fine imposed: {('$%s' % v.fine_amount) if v.fine_amount else '(none)'}",
          f"Fine due: {_day(v.fine_due_on) or '-'}   Paid: {_day(v.fine_paid_on) or '-'}",
          f"Posted to the owner's ledger: {_day(v.ledger_posted_on) or '-'} {v.ledger_reference}".rstrip()]
    if settings.SUSPENSION_ENABLED and v.suspension:
        h.append(f"Suspension imposed: {v.suspension} for {v.suspension_days} days")
    if v.legal_referred_on:
        h += ["", "LEGAL", f"Referred to the attorney: {_day(v.legal_referred_on)}; email sent {_when(v.legal_sent_on) or '(not sent)'} {v.legal_sent_to}".rstrip(),
              f"Reason: {v.legal_reason}"]
    h += ["", "(The meeting's minutes are not included: they can cover other owners' cases.)"]
    add("Hearing and decision", "hearing-and-decision/hearing-and-decision.txt", "\n".join(h).encode("utf-8"), _when(v.hearing_date),
        "Board vote, hearing, Committee decision, findings, imposition")

    # ---- messages with the owner and change history ---------------------------------------------------------------------------------
    files[f"{root}/messages/messages-with-the-owner.csv"] = _csv(
        ["When", "From", "Name", "Message", "Asked for more time", "Days asked", "Says it is fixed", "Decision"],
        [[_when(r.created_at), "owner" if r.from_resident else "association", _who(r.author) if r.author else "", r.message,
          "yes" if r.extension_requested else "", r.extension_days or "", "yes" if r.cure_reported else "", r.decision]
         for r in v.responses.select_related("author")])
    index.append(("Messages", "messages/messages-with-the-owner.csv", "", f"{v.responses.count()} message(s) between the owner and the Association"))
    evs = list(v.events.select_related("user"))
    files[f"{root}/history/case-history.csv"] = _csv(["When", "Who", "What happened"], [[_when(e.created_at), _who(e.user) if e.user else "", e.text] for e in evs])
    index.append(("Change history", "history/case-history.csv", "", f"{len(evs)} recorded step(s), including this download"))

    # ---- site-visit observations (internal: committee, Board and manager only) ---------------------------------------------------------
    SV, SEC = "Site-Visit-Observations", "Site-Visit Observations (internal)"
    if site_visits:
        obs_rows, photo_rows = [], []
        for ob in v.site_visits.select_related("corrects").prefetch_related("photos"):
            obs_rows.append([ob.pk, _when(ob.created_at), ob.author_name, ob.author_role, ob.corrects_id or "", ob.comment, ob.photos.count()])
            for n, p in enumerate(ob.photos.all(), 1):
                path = f"{SV}/photos/observation-{ob.pk}-photo-{n}.{p.file.name.rsplit('.', 1)[-1].lower() if '.' in p.file.name else 'jpg'}"
                try:
                    data = p.read_bytes()
                except Exception as e:      # noqa: BLE001
                    index.append((SEC, "(photo could not be read)", _when(p.created_at), f"observation #{ob.pk}: {e}"))
                    continue
                ok = hashlib.sha256(data).hexdigest() == p.sha256
                add(SEC, path, data, _when(ob.created_at), f"Photo from observation #{ob.pk} by {ob.author_name}"
                    + (f" (taken {_when(p.taken_at)})" if p.taken_at else "") + f" - fingerprint {'matches' if ok else 'DOES NOT MATCH'}")
                photo_rows.append([ob.pk, path, p.original_name, _when(p.taken_at), _when(p.created_at), p.size, p.sha256, "yes" if ok else "NO"])
        add(SEC, f"{SV}/observations.csv", _csv(["Observation", "Submitted", "By", "Role", "Corrects observation", "Comment", "Photos"], obs_rows), "",
            f"{len(obs_rows)} observation(s): internal notes, not committee findings")
        add(SEC, f"{SV}/photos-manifest.csv", _csv(["Observation", "File", "Original file name", "Taken (from the picture)", "Submitted", "Bytes",
                                                    "SHA-256 fingerprint", "File matches fingerprint"], photo_rows), "", f"{len(photo_rows)} photographs with fingerprints")
        add(SEC, f"{SV}/access-history.csv", _csv(["When", "Who", "Role", "What", "Observation", "Detail", "From address"],
            [[_when(a.at), a.user_name, a.role, a.get_action_display(), a.observation_id or "", a.detail, a.ip_address] for a in v.site_visit_access.all()]),
            "", "Who looked at, added to, opened a photograph from, or exported the observations")
    else:
        index.append((SEC, "(not included)", "", "Not included in this download: you may not see site-visit observations on this case."))

    # ---- index page -----------------------------------------------------------------------------------------------------------------
    by_section = {}
    for sec, path, when, what in index:
        by_section.setdefault(sec, []).append((path, when, what))
    parts = [f"<!doctype html><meta charset='utf-8'><title>Case file #{v.pk}</title>",
             "<style>body{font:15px Arial,sans-serif;max-width:900px;margin:24px auto;padding:0 12px}h2{margin-top:22px;border-bottom:1px solid #ccc}"
             "td,th{text-align:left;padding:4px 10px 4px 0;vertical-align:top}.w{color:#9a3412}</style>",
             f"<h1>Case file: #{v.pk} {escape(v.vtype.name)}</h1><p><b>{escape(o.display_name)}</b>, {escape(o.property_address)}<br>"
             f"Prepared {timezone.localtime():%Y-%m-%d %H:%M} by {escape(_who(user))} from the {escape(settings.APP_NAME)}.</p>"]
    for sec, items in by_section.items():
        parts.append(f"<h2>{escape(sec)}</h2><table><tr><th>File<th>Date<th>What it is</tr>")
        parts += [f"<tr><td><a href='{escape(p)}'>{escape(p)}</a><td>{escape(w)}<td>{escape(t)}</tr>" for p, w, t in items]
        parts.append("</table>")
    if missing_copy:
        parts.append("<h2 class='w'>Letters with no exact copy on file</h2><p>These were sent before exact copies were kept.</p><ul>"
                     + "".join(f"<li>{escape(x)}</li>" for x in missing_copy) + "</ul>")
    parts.append("<p>The fingerprint (SHA-256) of each letter is in letters/letters-manifest.csv: it proves the file has not changed since it was sent.</p>")
    parts.append("<p>manifest.csv lists every file in this download with its size and SHA-256 fingerprint.</p>")
    files[f"{root}/00-INDEX.html"] = "".join(parts).encode("utf-8")
    files[f"{root}/manifest.csv"] = _csv(["File", "Bytes", "SHA-256 fingerprint"],
                                         [[p.split("/", 1)[1], len(d), hashlib.sha256(d).hexdigest()] for p, d in sorted(files.items())])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            z.writestr(path, data)
    return buf.getvalue(), f"{root}.zip"
