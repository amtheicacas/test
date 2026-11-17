"""Print-ready mailing letters. Wording is a starting template — have association counsel review it."""
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape

from .models import Notice


def _p(text, style):
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def render_notice_pdf(notice: Notice) -> bytes:
    v = notice.violation
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=10.5, leading=14, spaceAfter=6)
    h = ParagraphStyle("h", parent=ss["Heading2"], spaceAfter=4)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11, textColor="#444444")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch,
                            title=f"{notice.get_kind_display()} #{v.pk}")
    s = [_p(settings.HOA_NAME, h), _p(settings.HOA_MAILING_ADDRESS, small), Spacer(1, 14),
         _p(timezone.localdate().strftime("%B %d, %Y"), body), Spacer(1, 6),
         _p(v.owner.mail_block, body), Spacer(1, 10),
         _p(f"RE: {notice.get_kind_display()} — {v.owner.property_address}  (Case #{v.pk})", h)]

    ref = f" ({v.vtype.reference})" if v.vtype.reference else ""
    s += [_p(f"Dear {v.owner.name}:", body)]

    if notice.kind == Notice.Kind.VIOLATION:
        s += [_p(f"On {v.observed_on:%B %d, %Y} the following was observed at your property, which appears to "
                 f"violate the community's governing documents{ref}:", body),
              _p(v.description, body), Spacer(1, 4),
              _p("Required cure:", ParagraphStyle("b", parent=body, fontName="Helvetica-Bold")),
              _p(v.cure_required, body),
              _p(f"Please cure this violation by {v.cure_deadline:%B %d, %Y}. If it is not cured by then, the "
                 "matter will be referred to the Board of Directors and may result in a fine after notice and a "
                 "hearing before the fining committee.", body)]
        for ph in v.photos.filter(from_resident=False)[:2]:
            try:
                img = Image(ph.image.path)
                img._restrictSize(5.5 * inch, 3.3 * inch)
                s += [Spacer(1, 6), img]
            except Exception:
                pass
    elif notice.kind == Notice.Kind.EXTENSION:
        s += [_p(f"Your request regarding the violation at {v.owner.property_address} ({v.vtype}) has been "
                 f"considered. The time to cure has been extended to {v.cure_deadline:%B %d, %Y}.", body)]
    elif notice.kind == Notice.Kind.HEARING:
        s += [_p(f"The Board of Directors has voted to proceed on the violation described below ({v.vtype}{ref}): "
                 f"{v.description}", body),
              _p(f"You are entitled to attend a hearing before the association's fining committee on "
                 f"{timezone.localtime(v.hearing_date):%A, %B %d, %Y at %I:%M %p}"
                 f"{' at ' + v.hearing_location if v.hearing_location else ''}. You may be represented by "
                 "counsel, present evidence, and respond to the allegations. A fine may be imposed only if the "
                 "committee, by majority vote, approves it. This notice is given at least "
                 f"{settings.HEARING_NOTICE_DAYS} days before the hearing.", body)]
    elif notice.kind == Notice.Kind.FINE:
        s += [_p(f"After a hearing, the fining committee approved a fine of ${v.fine_amount:,.2f} for the violation "
                 f"({v.vtype}) at {v.owner.property_address}. Please contact the association regarding payment.",
                 body)]

    s += [Spacer(1, 8),
          _p("Respond online, request more time, or send photos of the cure at:\n" + v.resident_url(), body),
          Spacer(1, 10), _p("Sincerely,\nBoard of Directors / Community Management\n" + settings.HOA_NAME, body)]
    doc.build(s)
    return buf.getvalue()
