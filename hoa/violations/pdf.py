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
RULE = ParagraphStyle("rule", parent=BODY, fontName="Helvetica-Bold", fontSize=9, leading=11.6)
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
    return timezone.localtime(dt).strftime("%m/%d/%y %I:%M %p")


def _photo(v):
    ph = v.photos.filter(from_resident=False).first()
    if not ph:
        return []
    try:
        img = Image(ph.image.path)
        img._restrictSize(2.0 * inch, 1.45 * inch)
        img.hAlign = "RIGHT"
        return [_p(_stamp(ph.shown_at), STAMP), img]
    except Exception:
        return []


def _signoff():
    return [Spacer(1, 2), _p("Very Truly Yours,", BODY),
            _p(f"On Behalf of the Board of Directors\n{settings.HOA_NAME}", BODY)]


def _respond_line(v):
    bits = [f"Respond, send photos of the corrected condition, or ask for more time at: {v.resident_url()}"]
    contact = ", ".join(x for x in (settings.HOA_CONTACT_EMAIL, settings.HOA_PHONE) if x)
    if contact:
        bits.append(f"Questions: {contact}. We aim to respond within 48-72 hours.")
    return _p("\n".join(bits), SMALL)


def _nov_body(v, notice_kind, to_name, mail_block, dated):
    """The Notice of Violation page(s) without the response form."""
    ref = f"{v.vtype.section} {v.vtype.standards_title}".strip() if v.vtype.section else (v.vtype.reference or "")
    left = [_p(f"{dated:%B %d, %Y}"), Spacer(1, 6), _p(mail_block)]
    top = Table([[left, _photo(v)]], colWidths=[4.1 * inch, 2.4 * inch])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    cure_by = f"{v.cure_deadline:%B %d, %Y}" if v.cure_deadline else f"within {v.cure_days} days of receipt of this notice"
    s = letterhead() + [top, Spacer(1, 8),
        _p(f"RE: {v.owner.property_address} located at {settings.HOA_NAME} Violation of the Covenants and Restrictions. "
           "All Governing Documents can be located and reviewed in the Homeowner Portal or at the Association's Website."),
        _p(f"Dear {to_name}:"),
        _p("Living in a planned community involves the consideration of all residents to abide by the deed restrictions. "
           "One reason for having such restrictions is to protect the rights of homeowners in the community, both yours "
           "and those of your neighbors. Another reason is to ensure all properties are attractively maintained to "
           "preserve the collective character and value of our neighborhood in a manner consistent with the original "
           "design of the development. Your property has been inspected and found to be in violation."),
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
             f"Correction Response Form within {v.cure_days} days of receipt of this notice. If the violation is not "
             "corrected, the matter may be referred to the Board of Directors. A fine or suspension may be imposed only "
             "after notice, a hearing and Compliance Committee approval."),
          _respond_line(v)]
    return s + _signoff()


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


def render_notice_pdf(notice: Notice, recipient: str = "owner") -> bytes:
    v = notice.violation
    to_tenant = recipient == "tenant" and v.owner.tenant_name
    to_name = v.owner.tenant_name if to_tenant else v.owner.name
    block = v.owner.tenant_mail_block if to_tenant else v.owner.mail_block
    city_line = ", ".join(x for x in (v.owner.city, " ".join(x for x in (v.owner.state, v.owner.zip_code) if x)) if x)
    if not to_tenant and v.owner.mail_line1:
        city_line = ", ".join(x for x in (v.owner.mail_city or v.owner.city,
                                          " ".join(x for x in (v.owner.mail_state or v.owner.state, v.owner.mail_zip or v.owner.zip_code) if x)) if x)
    block = f"{block}\n{city_line}" if city_line else block
    dated = timezone.localtime(notice.created_at).date()
    ref = f" ({v.vtype.reference})" if v.vtype.reference else ""
    title = notice.get_kind_display()

    if notice.kind in (K.VIOLATION, K.SECOND):
        s = _nov_body(v, notice.kind, to_name, block, dated) + _response_form(v, to_name, block, dated)
    else:
        s = letterhead() + [_p(f"{dated:%B %d, %Y}"), Spacer(1, 4), _p(block), Spacer(1, 8),
                            _p(f"RE: {title} - {v.owner.property_address}  (Case #{v.pk})", TITLE), _p(f"Dear {to_name}:")]
        if v.owner.tenant_name:
            s.append(_p(f"Copy also sent to {'the owner, ' + v.owner.name if to_tenant else 'the tenant, ' + v.owner.tenant_name}. "
                        "Owners are responsible for the actions of their tenants and guests.", SMALL))
        if notice.kind == K.EXTENSION:
            s.append(_p(f"The time to cure the violation at {v.owner.property_address} ({v.vtype}) has been extended to "
                        f"{v.cure_deadline:%B %d, %Y}."))
        elif notice.kind == K.HEARING:
            s += [_p(f"At its meeting on {v.board_meeting_date:%B %d, %Y}, the Board voted to levy {_what(v)} against you for "
                     f"the violation described in the attached Notice of Violation ({v.vtype}{ref}). This vote is only a "
                     "proposal. Nothing is final and nothing is owed unless a majority of the Compliance Committee approves it "
                     "after a hearing."),
                  _p("Hearing:", BOLD),
                  _p(f"{timezone.localtime(v.hearing_date):%A, %B %d, %Y at %I:%M %p}\nPlace: {v.hearing_location or 'to be announced'}"),
                  _p("You have the right to attend and speak, give evidence or a written statement, review and respond to the "
                     "information the Association relies on, and bring an attorney (please notify us in writing at least "
                     f"{settings.ATTORNEY_NOTICE_DAYS} days before the hearing). If you do not attend, the hearing will still go "
                     "ahead and the Committee may decide without you. This notice is given at least "
                     f"{settings.HEARING_NOTICE_DAYS} days before the hearing by certified mail (return receipt requested), "
                     "regular mail and email.")]
        elif notice.kind == K.FINE:
            bits = []
            if v.fine_amount:
                bits.append(f"a fine of ${v.fine_amount:,.2f}, payable within {settings.FINE_PAYMENT_DAYS} days of the date of this "
                            f"letter (by {v.fine_due_on:%B %d, %Y}). Please contact the Association for how to pay")
            if v.suspension:
                bits.append(f"a suspension: {v.suspension}")
            s.append(_p("Following the hearing, a majority of the Compliance Committee approved the Board's proposal. The following "
                        f"is imposed: {'; and '.join(bits)}. If the violation is not corrected and the fine paid, the matter may be "
                        "referred to the Association's attorney, and you may be responsible for attorney's fees and costs as allowed "
                        "by the governing documents and Florida law."))
        s += [_respond_line(v)] + _signoff()
        if notice.kind == K.HEARING:  # procedure: Notice of Hearing includes a copy of the earlier Notice of Violation
            first = v.notices.filter(kind=K.VIOLATION).first()
            first_date = timezone.localtime(first.created_at).date() if first else dated
            s += [PageBreak(), _p("ATTACHMENT - Copy of Notice of Violation", TITLE)]
            s += _nov_body(v, K.VIOLATION, to_name, block, first_date)

    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.55 * inch,
                      bottomMargin=0.55 * inch, title=f"{title} #{v.pk}").build(s)
    return buf.getvalue()
