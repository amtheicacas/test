"""Print-ready letters in the Association's courtesy-notice format. Wording is a starting template: have counsel review it.

Layout of a Notice of Violation (matches the Association's current letter): HOA letterhead, date and recipient on the
left with the violation photo and its timestamp on the right, RE: line, VIOLATION and COVENANTS blocks quoting the rule,
a Board of Directors sign-off, then a one-page Correction Response Form."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Notice

K = Notice.Kind
ss = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5, leading=12.2, spaceAfter=5)
BOLD = ParagraphStyle("bold", parent=BODY, fontName="Helvetica-Bold")
RULE = ParagraphStyle("rule", parent=BODY, fontName="Helvetica-Bold", fontSize=8.5, leading=10.9)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.5, leading=11, textColor="#444444")
HEAD = ParagraphStyle("head", parent=BODY, fontName="Helvetica", fontSize=13, leading=16, alignment=TA_CENTER, spaceAfter=1)
HEADSM = ParagraphStyle("headsm", parent=BODY, fontSize=9.5, leading=12, alignment=TA_CENTER, spaceAfter=0)
STAMP = ParagraphStyle("stamp", parent=SMALL, alignment=TA_RIGHT, spaceAfter=3)
TITLE = ParagraphStyle("title", parent=BOLD, fontSize=12, spaceAfter=6)


def _p(text, style=BODY):
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def hoa_address():
    s = settings
    parts = [s.HOA_ADDRESS_LINE1, " ".join(x for x in (f"{s.HOA_ADDRESS_CITY},", s.HOA_ADDRESS_STATE, s.HOA_ADDRESS_ZIP) if x)]
    return ", ".join(x for x in parts if x) if s.HOA_ADDRESS_LINE1 else ""


LOGO = Path(__file__).resolve().parent / "static" / "violations" / "heron-landing-logo.png"


def letterhead():
    """Logo on the left; name, address, phone and website centered beside it; rule underneath."""
    s = settings
    text = [_p(s.HOA_NAME, HEAD)]
    for t in (hoa_address(), f"Telephone: {s.HOA_PHONE}" if s.HOA_PHONE else "", s.HOA_WEBSITE):
        if t:
            text.append(_p(t, HEADSM))
    if LOGO.exists():
        logo = Image(str(LOGO), width=1.2 * inch, height=1.2 * inch * 130 / 265)
        head = Table([[logo, text, ""]], colWidths=[1.25 * inch, 4.2 * inch, 1.25 * inch])
        head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        lines = [head]
    else:
        lines = text
    return lines + [Spacer(1, 4), HRFlowable(width="100%", thickness=0.8, color=colors.black, spaceAfter=6)]


def _what(v):
    parts = []
    if v.proposed_fine:
        parts.append(f"a fine of ${v.proposed_fine:,.2f}")
    if v.proposed_suspension:
        parts.append(f"a suspension ({v.proposed_suspension})")
    return " and ".join(parts)


def _stamp(dt):
    d = timezone.localtime(dt)
    return f"{d.month}/{d.day}/{d:%y} {d.hour % 12 or 12}:{d:%M} {d:%p}"


def _caption(dt):
    d = timezone.localtime(dt)
    return f"{d:%A} - {d:%B} {d.day}, {d.year} - {d.hour % 12 or 12}:{d:%M} {d:%p}"


def _clock(dt):
    d = timezone.localtime(dt)
    return f"{d.hour % 12 or 12}:{d:%M} {'a.m.' if d.hour < 12 else 'p.m.'}"


def _photo(v, stamp_dt=None):
    """Top-right block: when the letter was generated, the first photo, and (under it) when the photo was taken."""
    ph = v.photos.filter(from_resident=False).first()
    if not ph:
        return []
    try:
        img = Image(ph.image.path)
        img._restrictSize(2.0 * inch, 1.45 * inch)
        img.hAlign = "RIGHT"
        return [_p(_stamp(stamp_dt or timezone.now()), STAMP), img, _p(_caption(ph.shown_at), ParagraphStyle("cap", parent=STAMP, fontSize=6.5, leading=8))]
    except Exception:
        return []


def _signoff():
    who = f"{settings.HOA_SIGNER_NAME}\n" if settings.HOA_SIGNER_NAME else ""
    return [Spacer(1, 2), _p("Very Truly Yours,", BODY),
            _p(f"{who}On Behalf of the Board of Directors\n{settings.HOA_NAME}", BODY)]


ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def num_words(n):
    """30 -> 'thirty', 45 -> 'forty-five' (the Resolution's letters write the number in words and digits)."""
    n = int(n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + (f"-{ONES[n % 10]}" if n % 10 else "")
    if n < 1000:
        return ONES[n // 100] + " hundred" + (f" {num_words(n % 100)}" if n % 100 else "")
    return str(n)


# Verbatim from the Fining Resolution (Exhibits 1 and 2): included only when the Lot is occupied by someone other than the owner.
OWNER_LIABILITY_NOV = ("This Notice is also being provided to the record Owner(s) of the Lot. Pursuant to Florida law and the "
    "Governing Documents, the Owner is jointly and severally liable for the conduct of his or her tenants, and the occupants of "
    "the Lot, as well as their guests, licensees, or invitees. Accordingly, if the violation is not corrected, this letter serves "
    "as notice that the Association may also take the above-referenced actions against the Owner(s), either directly, or jointly "
    "and severally.")
OWNER_LIABILITY_HEARING = ("This Notice is also being provided to the record Owner(s) of the Lot. Pursuant to Florida law and the "
    "Governing Documents, the Owner is jointly and severally liable for the conduct of his or her tenants, and the occupants of the "
    "Lot, as well as their guests, licensees, or invitees. Accordingly, the fine may also be imposed against the Owner(s), either "
    "directly, or jointly and severally. In cases where only a non-Owner (e.g., tenant, occupant, or invitee) is fined, the Owner "
    "shall be jointly and severally liable for payment of the fine.")
OWNER_SUSPENSION_SENTENCE = " Owners shall likewise be suspended from common facility use during the period of the suspension of a non-Owner."


def _respond_line(v):
    bits = [f"Respond, send photos of the corrected condition, or ask for more time at: {v.resident_url()}"]
    contact = ", ".join(x for x in (settings.HOA_CONTACT_EMAIL, settings.HOA_PHONE) if x)
    if contact:
        bits.append(f"Questions: {contact}. We aim to respond within 48-72 hours.")
    return _p("\n".join(bits), SMALL)


def _nov_body(v, notice_kind, to_name, mail_block, dated, stamp=None):
    """The Notice of Violation page(s) without the response form."""
    ref = f"{v.vtype.section} {v.vtype.standards_title}".strip() if v.vtype.section else (v.vtype.reference or "")
    left = [_p(f"{dated:%B %d, %Y}"), Spacer(1, 6), _p(mail_block)]
    top = Table([[left, _photo(v, stamp)]], colWidths=[4.1 * inch, 2.4 * inch])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    cure_by = f"{v.cure_deadline:%B %d, %Y}" if v.cure_deadline else f"within {v.cure_days} days of receipt of this notice"
    s = letterhead() + [_p("FIRST CLASS MAIL", BOLD), top, Spacer(1, 8),
        _p(f"RE: {v.owner.property_address} located at {settings.HOA_NAME} Violation of the Covenants and Restrictions. "
           "All Governing Documents can be located and reviewed in the Homeowner Portal or at the Association's Website."),
        _p(f"Dear {to_name}:"),
        _p("Living in a planned community involves the consideration of all residents to abide by the deed restrictions. "
           "One reason for having such restrictions is to protect the rights of homeowners in the community, both yours "
           "and those of your neighbors. Another reason is to ensure all properties are attractively maintained to "
           "preserve the collective character and value of our neighborhood in a manner consistent with the original "
           "design of the development. Your property has been inspected and found to be in violation. The Board of Directors "
           f"has information to believe that you ({'or your tenant(s), guest(s), or invitee(s)' if v.non_owner_occupied else 'or your guest(s) or invitee(s)'}) "
           "are in violation of the Association's Governing Documents."),
        Spacer(1, 2)]
    if notice_kind == K.SECOND:
        s.append(_p("This is a second notice. The deadline below replaces the one in your earlier notice.", BOLD))
    rows = [[_p("VIOLATION:", BOLD), _p(v.vtype.name, BOLD)],
            [_p("COVENANTS:", BOLD), _p(ref or "Declaration / Governing Documents", BOLD)]]
    t = Table(rows, colWidths=[1.1 * inch, 5.4 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1)]))
    s.append(t)
    if v.vtype.rule_text:
        s.append(_p(v.vtype.rule_text, RULE))
    s += [_p(f"Observed on {v.observed_on:%B %d, %Y}: {v.description}", RULE),
          _p(f"Required cure: {v.cure_required} Please correct this by {cure_by}.", RULE),
          _p("We appreciate your prompt attention to this matter. We find in most cases homeowners respond to this notice "
             "by immediately correcting the problem. If you can correct this situation in a reasonable length of time, we "
             f"thank you for making {settings.HOA_NAME} a better community, and you can consider this matter resolved."),
          _p("If you are unable to take corrective action, please submit a violation correction plan using the attached "
             f"Correction Response Form within {v.cure_days} days of receipt of this notice."),
          _p("Please be advised that you must comply with the above on or before the date stated. If you fail or refuse to comply, or "
             "if further violations occur, the Association may, with proper notice, pursue further action against you to enforce the "
             "Governing Documents. Such action may include (but is not limited to): imposing a fine against you and/or suspending your "
             "right to use certain Common Areas and facilities. A fine or suspension may be imposed only after notice, a hearing, and "
             "approval by the Compliance Committee. Further, the Association may also file a lawsuit against you to compel your "
             "compliance with the Governing Documents. If the Association takes any of the foregoing legal actions, it will seek to "
             "recover its attorneys' fees and costs as permitted by the Governing Documents and the Florida Homeowners' Association "
             "Act, Chapter 720 of the Florida Statutes.")]
    if v.non_owner_occupied:
        s.append(_p(OWNER_LIABILITY_NOV))
    return s + [_respond_line(v)] + _signoff()


def _response_form(v, to_name, mail_block, dated):
    block = mail_block.split("\n", 1)[1] if "\n" in mail_block else mail_block
    lab = ParagraphStyle("lab", parent=BODY)
    rows = [["Date:", f"{dated:%B %d, %Y}"], ["Name:", to_name], ["Property Address:", v.owner.property_address],
            ["Mailing Address:", block]]
    t = Table([[_p(a, lab), _p(b, lab)] for a, b in rows], colWidths=[1.5 * inch, 5.0 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    line = "_" * 78
    where = f"through the Association portal at {v.resident_url()}"
    if settings.HOA_CONTACT_EMAIL:
        where += f" or email this form to {settings.HOA_CONTACT_EMAIL}"
    elif hoa_address():
        where += f" or mail this form to {hoa_address()}"
    return [PageBreak()] + letterhead() + [
        Paragraph("Violation Correction Response Form", TITLE), t, Spacer(1, 6),
        Paragraph(f"<b>VIOLATION:</b>  {escape(v.vtype.name)}", BODY),
        Spacer(1, 4),
        _p("______    The non-compliance is not at this address."),
        _p("______    This issue has already been resolved."),
        _p(f"______    I cannot resolve this issue within {v.cure_days} days."),
        _p("My plan to resolve this violation is as follows:"), Spacer(1, 12),
        _p("Comments:"), _p(line), Spacer(1, 4), _p(line), Spacer(1, 4), _p(line), Spacer(1, 14),
        _p("Signature: " + "_" * 60), Spacer(1, 14),
        _p(f"Please respond {where}", ParagraphStyle("c", parent=BODY, alignment=TA_CENTER, fontSize=9))]


def _place_phrase(v):
    m = v.committee_meeting
    if m is None:
        return v.hearing_location or "a place to be announced"
    if m.zoom_link and m.location:
        return f"{m.location} and by Zoom (information below)"
    if m.zoom_link:
        return "by Zoom (information below)"
    return f"at {m.location}"


def _zoom_block(v):
    m = v.committee_meeting
    if not m or not m.zoom_link:
        return []
    lines = [f"<b>Join Zoom Meeting</b><br/>{escape(m.zoom_link)}"]
    if m.zoom_meeting_id:
        lines.append(f"Meeting ID: {escape(m.zoom_meeting_id)}")
    if m.zoom_passcode:
        lines.append(f"Passcode: {escape(m.zoom_passcode)}")
    if m.zoom_dial_in.strip():
        lines.append("One tap mobile<br/>" + "<br/>".join(escape(x) for x in m.zoom_dial_in.strip().splitlines()))
    return [Spacer(1, 6)] + [Paragraph(x, BODY) for x in lines]


def _hearing_notice(notice, v, to_name, block, dated):
    """Notice of Hearing (Fining Resolution Exhibit 2) in the Association's 'Hearing Panel Review' layout."""
    cert = "SENT BY CERTIFIED MAIL RETURN RECEIPT" + (" and by regular U.S. Mail" if settings.HEARING_REGULAR_COPY else "")
    if notice.tracking_number:
        cert += f"  #{notice.tracking_number}"
    left = [_p(f"{dated:%B %d, %Y}"), Spacer(1, 2), Paragraph("<b>Hearing Panel Review</b>", ParagraphStyle("hp", parent=BODY, fontSize=12, leading=14)),
            Spacer(1, 4), Paragraph(f"<u><b>{escape(cert)}</b></u>", ParagraphStyle("cert", parent=BODY, fontSize=8.5)), Spacer(1, 6),
            _p(block)]
    top = Table([[left, _photo(v, notice.created_at)]], colWidths=[4.1 * inch, 2.4 * inch])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    nov = v.notices.filter(kind=K.VIOLATION).first()
    nov_date = f"{timezone.localtime(nov.created_at):%m/%d/%Y}" if nov else "(date)"
    vote = (f"the Board voted to levy a fine against you in the amount of ${v.proposed_fine:,.2f}" if v.proposed_fine
            else "the Board voted to suspend your Lot's right to use certain Common Areas and facilities")
    when = f"during its meeting on {v.board_meeting_date:%m/%d/%Y}, " if v.board_meeting_date else ""
    s = letterhead() + [top, Spacer(1, 6),
        _p(f"RE: {v.owner.property_address} located at {settings.HOA_NAME}."), _p(f"Dear {to_name}:"),
        _p(f"The violation described in the Association's Notice of Violation dated {nov_date}, copy attached, has not been "
           f"corrected and/or has resumed. Accordingly, {when}{vote}.")]
    if v.proposed_suspension:
        days = v.proposed_suspension_days
        s.append(_p(f"The Association has {'also ' if v.proposed_fine else ''}suspended your Lot's right to use "
                    f"{v.proposed_suspension} for a period of {num_words(days)} ({days}) days."))
    h = timezone.localtime(v.hearing_date)
    place = _place_phrase(v)
    s += [Paragraph("Pursuant to Section 720.305(2)(b), Florida Statutes, there will be a hearing before the Compliance Committee on "
                    f"<b>{h:%B} {h.day}, {h.year}</b>, at <b>{_clock(h)}</b> {'at ' if not place.startswith(('by ', 'at ')) and ' and by Zoom' not in place else ''}"
                    f"<b>{escape(place)}</b>, to determine whether to confirm or reject the fine and/or suspension that the Board levied.", BODY),
          _p("You have a right to attend the hearing to respond, to present evidence, and to provide written or oral argument. You also have "
             "an opportunity at the hearing to review, challenge, and respond to any material considered by the Association. You have a right "
             "to be represented by counsel at the hearing. If you intend to appear with counsel, please notify the Association, in writing, "
             f"no later than seven ({settings.ATTORNEY_NOTICE_DAYS}) days prior to the hearing so that the Association has sufficient time "
             "to arrange for its counsel to be present should it choose to do so."),
          _p("If you fail to appear at the hearing, the fine and/or suspension, if confirmed by the Committee, will be imposed after a "
             "hearing has been conducted in your absence. If you have any questions about scheduling, please contact "
             f"{settings.HOA_CONTACT_NAME}" + (f" at {settings.HOA_PHONE}" if settings.HOA_PHONE else "")
             + (f" or {settings.HOA_CONTACT_EMAIL}" if settings.HOA_CONTACT_EMAIL else "") + "."),
          _p("In the event the Committee confirms the fine and/or suspension levied by the Board, the fine and/or suspension will be "
             "imposed. If the Committee rejects the fine and/or suspension, it will not be imposed. You will receive written notice of any "
             "fine and/or suspension imposed.")]
    if v.non_owner_occupied:
        s.append(_p(OWNER_LIABILITY_HEARING + (OWNER_SUSPENSION_SENTENCE if v.proposed_suspension else "")
                    + " Owners have the right to attend the Committee Hearing."))
    s += _signoff() + _zoom_block(v)
    first_stamp = nov.created_at if nov else notice.created_at
    s += [PageBreak(), _p("ATTACHMENT - Copy of Notice of Violation", TITLE)]
    s += _nov_body(v, K.VIOLATION, to_name, block, timezone.localtime(first_stamp).date(), first_stamp)
    return s


def render_notice_pdf(notice: Notice, recipient: str = "owner") -> bytes:
    """recipient: 'owner' (the mailing address if there is one), 'owner_property' (the physical address), or 'tenant'."""
    v = notice.violation
    o = v.owner
    to_tenant = recipient == "tenant" and o.has_tenant
    to_name = o.tenant_name if to_tenant else o.display_name
    block = o.address_block("tenant") if to_tenant else o.address_block("property" if recipient == "owner_property" else
                                                                         ("mailing" if o.has_separate_mailing else "property"))
    dated = timezone.localtime(notice.created_at).date()
    ref = f" ({v.vtype.reference})" if v.vtype.reference else ""
    title = notice.get_kind_display()

    if notice.kind == K.HEARING:
        s = _hearing_notice(notice, v, to_name, block, dated)
    elif notice.kind in (K.VIOLATION, K.SECOND):
        s = _nov_body(v, notice.kind, to_name, block, dated, notice.created_at) + _response_form(v, to_name, block, dated)
    else:
        s = letterhead() + [_p(f"{dated:%B %d, %Y}"), Spacer(1, 4), _p(block), Spacer(1, 8),
                            _p(f"RE: {title} - {v.owner.property_address}  (Case #{v.pk})", TITLE), _p(f"Dear {to_name}:")]
        if o.has_tenant:
            s.append(_p(f"Copy also sent to {'the owner, ' + o.display_name if to_tenant else 'the tenant, ' + o.tenant_name}. "
                        "Owners are responsible for the actions of their tenants and guests.", SMALL))
        if notice.kind == K.EXTENSION:
            s.append(_p(f"The time to cure the violation at {v.owner.property_address} ({v.vtype}) has been extended to "
                        f"{v.cure_deadline:%B %d, %Y}."))
        elif notice.kind == K.FINE:
            s.insert(len(letterhead()), _p(f"CERTIFIED MAIL # {notice.tracking_number or '_______________'}", BOLD))
            nov = v.notices.filter(kind=K.VIOLATION).first()
            hd = timezone.localtime(v.hearing_date) if v.hearing_date else None
            s.append(_p(f"Re: Notice of Violation dated {timezone.localtime(nov.created_at):%B %d, %Y}; Hearing held "
                        f"{hd:%B %d, %Y}." if nov and hd else "Re: Compliance Committee hearing.", BOLD))
            hdate = f"{hd:%B %d, %Y}" if hd else "(date)"
            if v.fine_amount:
                s.append(_p(f"During the Compliance Committee hearing on {hdate}, the Committee determined that the Board properly "
                            f"levied a fine against you in the amount of ${v.fine_amount:,.2f}, as authorized by law."))
            if v.suspension:
                lead = ("The Committee also confirmed" if v.fine_amount
                        else f"During the Compliance Committee hearing on {hdate}, the Committee confirmed")
                s.append(_p(f"{lead} to suspend your right to use {v.suspension} for a period of {num_words(v.suspension_days)} "
                            f"({v.suspension_days}) days, effective {v.suspension_effective_on:%B %d, %Y}."))
            imposed = []
            if v.fine_amount:
                imposed.append("As such, the fine is now imposed against you.")
            if v.suspension:
                imposed.append(("Likewise, as" if v.fine_amount else "As such, as") + f" of the date of this letter, the suspension is imposed "
                               f"for a period of {num_words(v.suspension_days)} ({v.suspension_days}) days. The suspension applies to all "
                               "Owners, tenants, occupants, guests, licensees, and invitees. It does not apply to the portion of the Common "
                               "Areas used to provide access or utility services to the parcel, and does not prohibit vehicular or pedestrian "
                               "ingress and egress, including the right to park.")
            s.append(_p(" ".join(imposed)))
            if v.fine_amount:
                s.append(_p(f"You must pay the fine no later than five ({settings.FINE_PAYMENT_DAYS}) days from the date of this letter "
                            f"(by {v.fine_due_on:%B %d, %Y}).", BOLD))
                if v.ledger_posted_on:
                    s.append(_p(f"The fine of ${v.fine_amount:,.2f} has been posted to your homeowner account ledger on "
                                f"{v.ledger_posted_on:%B %d, %Y}."))
                s.append(_p("You may remit payment of the fine to the Association" + (f", {settings.HOA_PAYMENT_ADDRESS}" if settings.HOA_PAYMENT_ADDRESS
                            else (f", {hoa_address()}" if hoa_address() else "")) + f". Checks should be made payable to {settings.HOA_PAYMENT_PAYABLE_TO}. "
                            "If you fail to pay the fine, or violate the Association's suspension order, the Association reserves all rights "
                            "available under law to collect the fine and enforce the suspension. These include (but are not limited to) filing "
                            "an action in a court of competent jurisdiction to recover the fine and/or enforce the suspension."))
            else:
                s.append(_p("If you violate the Association's suspension order, the Association reserves all rights available under law to "
                            "enforce the suspension, including filing an action in a court of competent jurisdiction."))
            s.append(_p("Thank you for your prompt attention to this matter."))
        s += [_respond_line(v)] + _signoff()
    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.55 * inch,
                      bottomMargin=0.55 * inch, title=f"{title} #{v.pk}").build(s)
    return buf.getvalue()
