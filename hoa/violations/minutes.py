"""Compliance Committee minutes, written by the app from what the committee entered on the meeting page (attendance, times, notes and one
decision per violation). They stay a draft after the meeting and are approved at the next meeting."""
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from .models import CommitteeMeeting, Violation
from .pdf import BODY, BOLD, SMALL, TITLE, letterhead


def _t(x):
    return f"{x:%I:%M %p}".lstrip("0") if x else "(time not recorded)"


def _d(x):
    return f"{x:%B} {x.day}, {x.year}"


def _lines(text):
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def build(m):
    """The minutes as a list of (heading, [paragraph, ...]) sections."""
    from django.utils import timezone
    start = timezone.localtime(m.starts_at)
    cases = list(m.violations.select_related("owner", "vtype").order_by("owner__property_address"))
    sections = []
    sections.append(("Meeting", [
        f"The Compliance Committee of {settings.HOA_NAME} met on {_d(start)} ({m.place}).",
        f"The meeting was called to order at {_t(m.called_to_order)}."]))
    rows = list(m.attendance.select_related("member").order_by("member__name"))
    label = lambda a: a.member.name + (f" ({a.member.get_role_display()})" if a.member.role != "member" else "")
    present = [label(a) for a in rows if a.status == "present"] + _lines(m.attendees)
    absent = [label(a) + (" (excused)" if a.status == "excused" else "") for a in rows if a.status != "present"] + _lines(m.members_absent)
    guests = _lines(m.guests)
    people = ["Committee members present: " + (", ".join(present) if present else "(not recorded)") + "."]
    if absent:
        people.append("Committee members absent: " + ", ".join(absent) + ".")
    quorum = len(present)
    if quorum:
        people.append(f"{quorum} member{'s were' if quorum != 1 else ' was'} present" + ("." if quorum >= 3 else " (fewer than the three needed to vote on a fine)."))
    if guests:
        people.append("Others present: " + ", ".join(guests) + ".")
    sections.append(("Attendance", people))
    approved_here = list(m.minutes_approved_here.all().order_by("starts_at"))
    sections.append(("Approval of prior minutes", [
        (f"The minutes of the {', '.join(_d(timezone.localtime(p.starts_at)) for p in approved_here)} meeting{'s' if len(approved_here) > 1 else ''} "
         "were approved.") if approved_here else "No prior minutes were approved at this meeting."]))
    hearings = []
    word = {"confirm": "confirmed the fine as proposed", "amend": "confirmed the fine in a changed amount", "waive": "waived the violation",
            "reject": "rejected the fine"}
    for i, v in enumerate(cases, 1):
        bits = [f"{i}. {v.owner.property_address} ({v.owner.display_name}): {v.vtype.name}."]
        if v.proposed_fine:
            bits.append(f"The Board issued a fine of ${v.proposed_fine:,.2f}.")
        bits.append("The owner or a representative attended the hearing." if v.committee_owner_present else "The owner did not attend the hearing.")
        if v.committee_findings:
            bits.append("Findings: " + v.committee_findings.split("\n[Returned")[0].strip())
        if v.committee_outcome:
            d = f"Decision: the Committee {word[v.committee_outcome]}"
            if v.committee_outcome == "amend" and v.committee_amended_fine:
                d += f" (${v.committee_amended_fine:,.2f})"
            if v.committee_votes_for is not None:
                d += f", by a vote of {v.committee_votes_for} to {v.committee_votes_against}"
            bits.append(d + ".")
        else:
            bits.append("No decision has been recorded.")
        hearings.append(" ".join(bits))
    sections.append(("Violations heard", hearings or ["No violations were assigned to this meeting."]))
    if m.notes.strip():
        sections.append(("Other business and notes", _lines(m.notes)))
    sections.append(("Adjournment", [f"The meeting was adjourned at {_t(m.adjourned)}."]))
    sections.append(("Approval", ["These minutes were approved on " + _d(m.minutes_approved_on) + "." if m.minutes_status == "approved"
                                   else "DRAFT: prepared from the Committee's entries; to be approved at the next meeting."]))
    return sections


def render_pdf(m):
    from django.utils import timezone
    s = letterhead() + [Paragraph("Compliance Committee: Minutes of the Meeting of " + _d(timezone.localtime(m.starts_at)), TITLE)]
    if m.minutes_status != "approved":
        s.append(Paragraph("<b>DRAFT: not yet approved</b>", BODY))
    for head, paras in build(m):
        s.append(Paragraph(escape(head), BOLD))
        s += [Paragraph(escape(p), BODY) for p in paras]
    s += [Spacer(1, 26), Paragraph("Approved: ______________________________   Date: ______________", BODY),
          Paragraph("Committee Chair / Secretary", SMALL)]
    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.55 * inch, bottomMargin=0.55 * inch,
                      title="Compliance Committee minutes").build(s)
    return buf.getvalue()
