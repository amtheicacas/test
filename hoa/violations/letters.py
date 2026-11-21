"""The saved, exact copy of every letter sent, and attaching the saved original Notice of Violation to the hearing notice."""
import hashlib
import io

from django.core.files.base import ContentFile

from .models import Notice, SentLetter

K = Notice.Kind
EXACT = (SentLetter.Origin.SENT, SentLetter.Origin.UPLOADED)     # copies we can attach as "the exact notice that was sent"


def save_sent_copy(notice, pdf, recipient, channel, to="", name="", user=None, tracking="", piece=None):
    """Keep the exact bytes of a letter that was just emailed or handed to the mail service. Returns the SentLetter. A failure to save is
    raised, not hidden: a letter without its saved copy is a gap in the case file, so the caller records it."""
    digest = hashlib.sha256(pdf).hexdigest()
    letter = SentLetter(violation=notice.violation, notice=notice, kind=notice.kind, recipient=recipient, recipient_name=name[:200],
                        sent_to=to, channel=channel, tracking_number=tracking or "", mail_piece=piece, sent_by=user,
                        sha256=digest, size=len(pdf))
    twin = SentLetter.objects.filter(sha256=digest).exclude(file="").first()     # identical bytes (e.g. the emailed and the mailed copy)
    if twin is not None and twin.intact():
        letter.file = twin.file.name                                              # point at the file we already hold: nothing to store twice
    else:
        letter.file.save("letter.pdf", ContentFile(pdf), save=False)
    letter.save()
    return letter


def original_notice(violation):
    """The ORIGINAL Notice of Violation (not a second notice), or None."""
    return violation.notices.filter(kind=K.VIOLATION).order_by("pk").first()


def original_notice_copy(violation, recipient="owner"):
    """The saved exact copy of the original Notice of Violation to attach for this recipient version ('owner', 'owner_property', 'tenant').
    The same version if there is one, else the same person's other version (a letter to the property address -> the owner's), else any saved
    copy of the original. None when no exact copy was ever saved (then the hearing notice falls back to a rebuilt copy)."""
    nov = original_notice(violation)
    if nov is None:
        return None
    copies = list(nov.sent_copies.filter(origin__in=EXACT).order_by("sent_at", "pk"))
    if not copies:
        return None
    group = "tenant" if recipient == "tenant" else "owner"
    for test in (lambda c: c.recipient == recipient,
                 lambda c: (c.recipient == "tenant") == (group == "tenant") and c.recipient in ("owner", "owner_property", "tenant"),
                 lambda c: True):
        for c in copies:
            if test(c):
                return c
    return None


def merge_pdfs(*pdfs):
    """One PDF made of the given PDFs (bytes), in order."""
    from pypdf import PdfReader, PdfWriter
    out = PdfWriter()
    for data in pdfs:
        out.append(PdfReader(io.BytesIO(data)))
    buf = io.BytesIO()
    out.write(buf)
    return buf.getvalue()


def without_saved_copy():
    """Notices that have no saved exact copy at all: everything sent before copies were kept. Newest last."""
    return Notice.objects.filter(sent_copies__isnull=True).select_related("violation__owner").order_by("created_at")
