"""Print-ready mailing letters. Wording follows the Heron Landing procedure; have association counsel review it."""
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from .models import Notice

K = Notice.Kind
ss = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=ss["Normal"], fontSize=10.5, leading=14, spaceAfter=6)
H = ParagraphStyle("h", parent=ss["Heading2"], spaceAfter=4)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.5, leading=11, textColor="#444444")
BOLD = ParagraphStyle("bold", parent=BODY, fontName="Helvetica-Bold")


def _p(text, style=BODY):
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _what(v):
    parts = []
    if v.proposed_fine:
        parts.append(f"a fine of ${v.proposed_fine:,.2f}")
    if v.proposed_suspension:
        parts.append(f"a suspension ({v.proposed_suspension})")
    return " and ".join(parts)


def _nov_story(v, kind, dated, recipient_name, ref):
    s = [_p(f"On {v.observed_on:%B %d, %Y} the following was observed at {v.owner.property_address}, which "
            f"appears to violate the community's governing documents{ref}:"),
         _p(v.description), Spacer(1, 4), _p("Required cure:", BOLD), _p(v.cure_required)]
    if kind == K.SECOND:
        s.append(_p("This is a second notice. Your earlier deadline has been replaced by the new deadline below."))
    s.append(_p(f"Please cure this violation by {v.cure_deadline:%B %d, %Y}. If it is not cured, the Board of "
                "Directors may vote to propose a fine and/or suspension. Nothing can be imposed unless you are "
                "given notice of a hearing and a majority of the Compliance Committee, a group of owners, approves "
                "it. If you believe this notice is a mistake, or need more time, please contact the Manager in "
                "writing before your deadline."))
    for ph in v.photos.filter(from_resident=False)[:2]:
        try:
            img = Image(ph.image.path)
            img._restrictSize(5.5 * inch, 3.3 * inch)
            s += [Spacer(1, 6), img]
        except Exception:
            pass
    return s


def _letter_head(v, dated, recipient_block, title):
    return [_p(settings.HOA_NAME, H), _p(settings.HOA_MAILING_ADDRESS, SMALL), Spacer(1, 12),
            _p(f"{dated:%B %d, %Y}"), Spacer(1, 4), _p(recipient_block), Spacer(1, 8),
            _p(f"RE: {title} — {v.owner.property_address}  (Case #{v.pk})", H)]


def _footer(v, notice):
    return [Spacer(1, 6),
            _p("You can view this notice, reply in writing, report that the work is done (photos help), or ask for more "
               f"time here:\n{v.resident_url()}\nOr contact {settings.MANAGER_NAME}, {settings.MANAGER_EMAIL}, "
               f"{settings.OFFICE_PHONE}. We aim to respond within 48–72 hours.", SMALL),
            Spacer(1, 10), _p(f"Sincerely,\nBoard of Directors / {settings.MANAGER_NAME}, Community Manager\n"
                              f"{settings.HOA_NAME}")]


def render_notice_pdf(notice: Notice, recipient: str = "owner") -> bytes:
    v = notice.violation
    to_tenant = recipient == "tenant" and v.owner.tenant_name
    to_name = v.owner.tenant_name if to_tenant else v.owner.name
    block = v.owner.tenant_mail_block if to_tenant else v.owner.mail_block
    ref = f" ({v.vtype.reference})" if v.vtype.reference else ""
    dated = timezone.localtime(notice.created_at).date()
    title = notice.get_kind_display()

    s = _letter_head(v, dated, block, title) + [_p(f"Dear {to_name}:")]
    if v.owner.tenant_name:
        s.append(_p(f"Copy also sent to {'the owner, ' + v.owner.name if to_tenant else 'the tenant, ' + v.owner.tenant_name}. "
                    "Owners are responsible for the actions of their tenants and guests.", SMALL))

    if notice.kind in (K.VIOLATION, K.SECOND):
        s += _nov_story(v, notice.kind, dated, to_name, ref)
    elif notice.kind == K.EXTENSION:
        s.append(_p(f"The time to cure the violation at {v.owner.property_address} ({v.vtype}) has been extended "
                    f"to {v.cure_deadline:%B %d, %Y}."))
    elif notice.kind == K.HEARING:
        s += [_p(f"At its meeting on {v.board_meeting_date:%B %d, %Y}, the Board voted to levy {_what(v)} against you "
                 f"for the violation described in the attached Notice of Violation ({v.vtype}{ref}). This vote is only a "
                 "proposal. Nothing is final and nothing is owed unless a majority of the Compliance Committee approves "
                 "it after a hearing."),
              _p("Hearing:", BOLD),
              _p(f"{timezone.localtime(v.hearing_date):%A, %B %d, %Y at %I:%M %p}\n"
                 f"Place: {v.hearing_location or 'to be announced'}"),
              _p("You have the right to attend and speak, give evidence or a written statement, review and respond to "
                 "the information the Association relies on, and bring an attorney (please notify us in writing at "
                 f"least {settings.ATTORNEY_NOTICE_DAYS} days before the hearing). If you do not attend, the hearing "
                 "will still go ahead and the Committee may decide without you. This notice is given at least "
                 f"{settings.HEARING_NOTICE_DAYS} days before the hearing by certified mail (return receipt requested), "
                 "regular mail and email.")]
    elif notice.kind == K.FINE:
        bits = []
        if v.fine_amount:
            bits.append(f"a fine of ${v.fine_amount:,.2f}, payable within {settings.FINE_PAYMENT_DAYS} days of the date of "
                        f"this letter (by {v.fine_due_on:%B %d, %Y}). Contact {settings.MANAGER_NAME} for how to pay")
        if v.suspension:
            bits.append(f"a suspension: {v.suspension}")
        s.append(_p("Following the hearing, a majority of the Compliance Committee approved the Board's proposal. "
                    f"The following is imposed: {'; and '.join(bits)}. If the violation is not corrected and the fine "
                    "paid, the matter may be referred to the Association's attorney, and you may be responsible for "
                    "attorney's fees and costs as allowed by the governing documents and Florida law."))

    s += _footer(v, notice)

    if notice.kind == K.HEARING:  # procedure: Notice of Hearing includes a copy of the earlier Notice of Violation
        first = v.notices.filter(kind=K.VIOLATION).first()
        first_date = timezone.localtime(first.created_at).date() if first else dated
        s += [PageBreak(), _p("ATTACHMENT — Copy of Notice of Violation", H)]
        s += _letter_head(v, first_date, block, K.VIOLATION.label)[:-1]
        s += [_p(f"RE: {K.VIOLATION.label} — {v.owner.property_address}  (Case #{v.pk})", H)]
        s += _nov_story(v, K.VIOLATION, first_date, to_name, ref)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=0.8 * inch,
                            bottomMargin=0.8 * inch, title=f"{title} #{v.pk}")
    doc.build(s)
    return buf.getvalue()
