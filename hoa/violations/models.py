import re
import secrets
from datetime import date


def short_date(d):
    """'Nov 5, 2026' (the '%-d' strftime flag does not work on Windows)."""
    return f"{d:%b} {d.day}, {d.year}"

from django.conf import settings
from django.db import models
from django.urls import reverse


def photo_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"photos/{secrets.token_hex(16)}.{ext}"


def split_emails(text):
    """'a@x.com; b@x.com' -> ['a@x.com', 'b@x.com'] (Vantaca separates several addresses with ';')."""
    seen, out = set(), []
    for e in re.split(r"[;,\s]+", text or ""):
        e = e.strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def _norm_addr(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


class Owner(models.Model):
    name = models.CharField(max_length=200, help_text="Homeowner name 1 (as Vantaca lists it).")
    second_owner_name = models.CharField(max_length=200, blank=True, help_text="Homeowner name 2. Added to letters as the second person.")
    property_address = models.CharField(max_length=250, unique=True, help_text="The physical address. Default mailing address.")
    lot_no = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True, help_text="Leave blank to send by mail only.")
    additional_emails = models.TextField(blank=True, help_text="More owner emails, separated by ';'. All of them get violation emails.")
    phone = models.CharField(max_length=50, blank=True)
    mailing_address = models.TextField(blank=True, help_text="If filled in, US Mail goes here AND to the property address.")
    notes = models.TextField(blank=True)
    city = models.CharField(max_length=80, default="Sarasota")
    state = models.CharField(max_length=2, default="FL")
    zip_code = models.CharField(max_length=10, blank=True)
    mail_line1 = models.CharField("mailing street", max_length=200, blank=True, help_text="Split out of the mailing address for the print service.")
    mail_city = models.CharField(max_length=80, blank=True)
    mail_state = models.CharField(max_length=2, blank=True)
    mail_zip = models.CharField(max_length=10, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    co_owner_email = models.EmailField(blank=True, help_text="Another owner's email. Gets notices and can have their own login.")
    tenant_name = models.CharField(max_length=200, blank=True, help_text="If the home is rented, notices also go to the tenant(s).")
    tenant_email = models.EmailField(blank=True)
    tenant_additional_emails = models.TextField(blank=True, help_text="More tenant emails, separated by ';'.")
    tenant_phone = models.CharField(max_length=50, blank=True)
    tenant_start = models.DateField(null=True, blank=True, help_text="Lease start. Before this date the tenant isn't treated as living there.")
    tenant_end = models.DateField(null=True, blank=True, help_text="Lease end. After this date the tenant no longer gets notices.")
    active = models.BooleanField(default=True, help_text="Untick when the owner has left (the sync does this). Disables their login.")
    external_id = models.CharField(max_length=100, unique=True, null=True, blank=True,
                                   help_text="Vantaca Homeowner ID (set by the import / sync).")
    account_number = models.CharField(max_length=50, blank=True, help_text="Vantaca Account Number.")
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["property_address"]

    def __str__(self):
        return f"{self.name} — {self.property_address}"

    # ---- who gets the notice ----
    @property
    def display_name(self):
        """Name on letters: name 1, plus name 2 when there is one (and it isn't already part of name 1)."""
        n2 = self.second_owner_name.strip()
        if n2 and _norm_addr(n2) not in _norm_addr(self.name):
            return f"{self.name} & {n2}"
        return self.name

    @property
    def all_emails(self):
        """Every owner email: the main one, the ';'-separated extras and the co-owner's."""
        return split_emails(";".join(x for x in (self.email, self.additional_emails, self.co_owner_email) if x))

    @property
    def tenant_emails(self):
        return split_emails(";".join(x for x in (self.tenant_email, self.tenant_additional_emails) if x))

    @property
    def has_tenant(self):
        """A tenant is on record AND the lease (if dates are given) covers today."""
        if not self.tenant_name.strip():
            return False
        today = date.today()
        return not ((self.tenant_start and self.tenant_start > today) or (self.tenant_end and self.tenant_end < today))

    @property
    def tenant_lease_ended(self):
        return bool(self.tenant_name.strip() and self.tenant_end and self.tenant_end < date.today())

    # ---- where it is mailed ----
    @property
    def has_separate_mailing(self):
        """A mailing address on file that is really somewhere else (not just the property's own street again)."""
        m, p = _norm_addr(self.mailing_address), _norm_addr(self.property_address)
        return bool(m) and m not in p and p not in m

    def _property_postal(self, name):
        return {"name": name, "line1": self.property_address, "city": self.city, "state": self.state, "zip": self.zip_code}

    def postal(self, kind="property"):
        """Structured address for the print/mail provider. kind: 'mailing' | 'property' | 'tenant'."""
        if kind == "tenant":
            return self._property_postal(self.tenant_name)
        if kind == "mailing":
            return {"name": self.display_name, "line1": self.mail_line1, "city": self.mail_city, "state": self.mail_state,
                    "zip": self.mail_zip}
        return self._property_postal(self.display_name)

    def owner_mail_kinds(self):
        """US Mail for the owner goes to the mailing address (when there is one) AND the property address."""
        return ["mailing", "property"] if self.has_separate_mailing else ["property"]

    def address_block(self, kind="property"):
        """Printed name-and-address block. kind: 'mailing' | 'property' | 'tenant'."""
        csz = ", ".join(x for x in (self.city, " ".join(x for x in (self.state, self.zip_code) if x)) if x)
        if kind == "mailing":
            return f"{self.display_name}\n{self.mailing_address.strip()}"
        name = self.tenant_name if kind == "tenant" else self.display_name
        return f"{name}\n{self.property_address}" + (f"\n{csz}" if csz else "")

    @property
    def mail_block(self):
        return self.address_block("mailing" if self.has_separate_mailing else "property")

    @property
    def tenant_mail_block(self):
        return self.address_block("tenant")


class ViolationType(models.Model):
    name = models.CharField(max_length=200)
    reference = models.CharField(max_length=200, blank=True,
                                 help_text="Governing document cite, e.g. 'Declaration Art. VII §3(b)'.")
    default_cure_days = models.PositiveIntegerField(default=15)
    default_fine = models.DecimalField(max_digits=8, decimal_places=2, default=100)
    # Filled from the Community Standards (load_standards); blank for hand-made types
    source = models.CharField(max_length=30, default="Community Standards", help_text="Which document the rule comes from: Community Standards or Declaration.")
    code = models.CharField(max_length=30, unique=True, null=True, blank=True, help_text="e.g. 3.22-timing, or D-5.4 for the Declaration")
    section = models.CharField(max_length=10, blank=True, help_text="Community Standards section, e.g. 3.22")
    standards_title = models.CharField(max_length=200, blank=True)
    category = models.CharField(max_length=60, blank=True)
    rule_text = models.TextField(blank=True, help_text="Verbatim text of the rule, quoted in the notice.")
    observed_template = models.TextField(blank=True, help_text="Drafted 'what was observed'. [Brackets] must be filled in.")
    cure_template = models.TextField(blank=True, help_text="Drafted cure. [Brackets] must be filled in.")
    keywords = models.TextField(blank=True, help_text="Words people use for this issue (search only).")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CommitteeMeeting(models.Model):
    """A Compliance Committee meeting (homeowner hearing). Aged cases are batched onto one meeting."""
    starts_at = models.DateTimeField()
    zoom_link = models.URLField(blank=True)
    zoom_meeting_id = models.CharField(max_length=40, blank=True)
    zoom_passcode = models.CharField(max_length=40, blank=True)
    zoom_dial_in = models.TextField(blank=True, help_text="'One tap mobile' lines from the Zoom invitation, one per line.")
    location = models.CharField(max_length=250, blank=True, help_text="In-person place, if any.")
    notes = models.TextField(blank=True)
    attendees = models.TextField(blank=True, help_text="Committee members present.")
    minutes = models.FileField(upload_to="minutes/", blank=True, help_text="The meeting minutes (PDF or Word).")

    class Results(models.TextChoices):
        OPEN = "open", "Committee working"
        SUBMITTED = "submitted", "Awaiting Board review"
        PROCESSED = "processed", "Board reviewed"

    results_status = models.CharField(max_length=10, choices=Results.choices, default=Results.OPEN)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return f"Committee meeting {self.starts_at:%b %d, %Y %I:%M %p}"

    @property
    def place(self):
        return " · ".join(x for x in [f"Zoom: {self.zoom_link}" if self.zoom_link else "", self.location] if x) or "to be announced"

    @property
    def has_occurred(self):
        from django.utils import timezone
        return self.starts_at <= timezone.now()


class Violation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        NOTICE_SENT = "notice_sent", "Cure period running"
        PENDING_BOARD = "pending_board", "Awaiting board vote"
        HEARING_NEEDED = "hearing_needed", "Board approved — schedule hearing"
        HEARING_SCHEDULED = "hearing_scheduled", "Sent to Compliance Committee"
        ON_HOLD = "on_hold", "On hold"
        LEDGER_PENDING = "ledger_pending", "Committee decided: awaiting Board review"
        WAIVED = "waived", "Waived by the Committee (closed)"
        FINE_APPROVED = "fine_approved", "Imposed by committee — awaiting payment / cure"
        FINE_REJECTED = "fine_rejected", "Rejected by committee"
        REFERRED_LEGAL = "referred_legal", "Referred to attorney"
        CLOSED = "closed", "Closed (cured and paid)"
        CURED = "cured", "Cured"
        DISMISSED = "dismissed", "Dismissed"

    OPEN = [Status.DRAFT, Status.NOTICE_SENT, Status.PENDING_BOARD, Status.HEARING_NEEDED,
            Status.HEARING_SCHEDULED, Status.LEDGER_PENDING,
            Status.FINE_APPROVED, Status.ON_HOLD]

    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name="violations")
    vtype = models.ForeignKey(ViolationType, on_delete=models.PROTECT, verbose_name="violation type")
    observed_on = models.DateField(default=date.today)
    description = models.TextField(help_text="What was observed.")
    cure_required = models.TextField(help_text="What the owner must do to cure.")
    cure_days = models.PositiveIntegerField(default=15, help_text="Days allowed to cure from the notice date. Enter 0 for \"immediately\".")
    cure_deadline = models.DateField(null=True, blank=True)
    extended_days = models.PositiveIntegerField(default=0, help_text="Total days added to the cure period by approved extensions.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    token = models.CharField(max_length=64, unique=True, editable=False, default=secrets.token_urlsafe)

    # "Immediate Issue" options: any of these lets the Board act before a cure period would run out
    class Timing(models.TextChoices):
        STANDARD = "standard", "Standard: email now, mail after the delay"
        URGENT = "urgent", "Urgent: email and mail now"
    issuance_timing = models.CharField(max_length=10, choices=Timing.choices, default=Timing.STANDARD)
    non_curable = models.BooleanField("Non curable violation", default=False,
                                      help_text="Can't be undone by corrective action, so no cure period is given.")
    safety_issue = models.BooleanField("Safety issue", default=False, help_text="Threatens health/safety — Board may act before the cure deadline.")
    board_emergency = models.BooleanField("Board declared emergency", default=False)
    # Board stage: the vote only PROPOSES a fine and/or suspension
    board_meeting_date = models.DateField(null=True, blank=True)
    proposed_fine = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    proposed_suspension = models.CharField(max_length=300, blank=True, help_text="Which Common Areas/facilities, e.g. 'pool and clubhouse'.")
    proposed_suspension_days = models.PositiveIntegerField(null=True, blank=True)
    board_votes_for = models.PositiveSmallIntegerField(null=True, blank=True)
    board_votes_against = models.PositiveSmallIntegerField(null=True, blank=True)
    held_from = models.CharField(max_length=20, blank=True)
    hold_reason = models.CharField(max_length=300, blank=True)
    held_on = models.DateField(null=True, blank=True)
    reinspect_on = models.DateField(null=True, blank=True, help_text="Set when staff mark the case for re-inspection (the owner says it is fixed); it keeps aging until verified.")
    hold_until = models.DateField(null=True, blank=True, help_text="The hold lifts itself on this date and the cure period starts over.")
    off_hold_on = models.DateField(null=True, blank=True, help_text="When the last hold ended (the cure period restarted that day).")
    # Committee stage
    committee_meeting = models.ForeignKey(CommitteeMeeting, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="violations")
    hearing_date = models.DateTimeField(null=True, blank=True)
    hearing_location = models.CharField(max_length=250, blank=True)
    committee_votes_for = models.PositiveSmallIntegerField(null=True, blank=True)
    committee_votes_against = models.PositiveSmallIntegerField(null=True, blank=True)
    OUTCOMES = [("confirm", "Fine confirmed (as proposed)"), ("amend", "Fine confirmed, amount changed"),
                ("waive", "Waived / resolved: close the violation"), ("reject", "Fine rejected")]
    committee_outcome = models.CharField(max_length=8, blank=True, choices=OUTCOMES)
    committee_amended_fine = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    board_outcome = models.CharField(max_length=8, blank=True, choices=OUTCOMES, help_text="What the Board finally did with the committee's recommendation.")
    board_final_fine = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    board_review_note = models.TextField(blank=True)
    board_reviewed_on = models.DateField(null=True, blank=True)
    board_modified = models.BooleanField(default=False)
    committee_findings = models.TextField(blank=True, help_text="Brief written findings of fact; quoted in the Notice of committee decision.")
    committee_cure_by = models.DateField(null=True, blank=True, help_text="If the violation is cured by this date, no fine or suspension is imposed.")
    fine_amount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="Imposed amount (set when committee approves).")
    suspension = models.CharField(max_length=300, blank=True)
    suspension_days = models.PositiveIntegerField(null=True, blank=True)
    suspension_effective_on = models.DateField(null=True, blank=True)
    fine_due_on = models.DateField(null=True, blank=True)
    fine_paid_on = models.DateField(null=True, blank=True)
    ledger_posted_on = models.DateField(null=True, blank=True, help_text="When the fine was posted to the owner's ledger.")
    ledger_reference = models.CharField(max_length=120, blank=True, help_text="Vantaca transaction / reference number.")
    cured_on = models.DateField(null=True, blank=True)
    legal_referred_on = models.DateField(null=True, blank=True)
    legal_path = models.CharField(max_length=12, blank=True, help_text="after_fine: fine imposed but still not corrected; direct: sent to the attorney because of the nature of the violation.")
    legal_reason = models.TextField(blank=True)
    legal_sent_on = models.DateTimeField(null=True, blank=True, help_text="When the reviewed email package went to the attorney.")
    legal_sent_to = models.CharField(max_length=200, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.pk} {self.vtype} @ {self.owner.property_address}"

    @property
    def is_open(self):
        return self.status in self.OPEN

    @property
    def days_left(self):
        if self.cure_deadline and self.status == self.Status.NOTICE_SENT:
            return (self.cure_deadline - date.today()).days
        return None

    STEPS = ["Notice issued", "Cure period", "Fine issued by Board",
             "Violation sent to Compliance Committee / Homeowner Hearing", "Fine Imposed & Posted to Ledger", "Closed"]

    def stage_index(self):
        St = self.Status
        st = self.held_from if self.status == St.ON_HOLD and self.held_from else self.status
        return {St.DRAFT: -1, St.NOTICE_SENT: 1, St.PENDING_BOARD: 2, St.HEARING_NEEDED: 2, St.HEARING_SCHEDULED: 3,
                St.LEDGER_PENDING: 4, St.FINE_APPROVED: 4, St.REFERRED_LEGAL: 4}.get(st, 5)

    def stage_steps(self):
        """[(label, 'done'|'current'|'todo'|'skipped'|'legal')] for the progress tracker shown to owners and staff.
        'skipped' marks a step the case never went through (cured early, or sent straight to the attorney); a case referred to
        legal also gets a 'Referred to attorney' step showing where it stands."""
        St = self.Status
        cur = self.stage_index()
        legal = self.status == St.REFERRED_LEGAL
        ended = self.status in (St.REFERRED_LEGAL, St.CURED, St.DISMISSED, St.FINE_REJECTED, St.CLOSED, St.WAIVED)
        evidence = {
            2: bool(self.board_votes_for is not None or self.board_meeting_date or self.proposed_fine or self.proposed_suspension),
            3: bool(self.committee_votes_for is not None or self.committee_meeting_id),
            4: bool(self.fine_due_on or self.ledger_posted_on or self.suspension or (self.fine_amount and self.status in (St.FINE_APPROVED, St.CLOSED))),
        }
        out = []
        for i, label in enumerate(self.STEPS):
            if legal and i == 5:
                out.append(("Referred to attorney", "legal"))
            if i == cur and not ended:
                state = "current"
            elif i < cur or (cur == 5 and i == 5 and not legal):
                state = "skipped" if (ended and i in evidence and not evidence[i]) else "done"
            elif ended and i in evidence and i >= 2 and not evidence[i]:
                state = "skipped"
            else:
                state = "todo"
            if legal and i < 5 and i >= 2 and not evidence[i]:
                state = "skipped"
            if legal and i == 4 and evidence[4]:
                state = "done"
            if self.status == St.FINE_REJECTED and i == 4:
                state = "skipped"
            out.append((label, state))
        return out

    ICONS = ["\u2709", "\u23f3", "\u2611", "\U0001f465", "$", "\u2714"]   # notice, cure period, board, committee, fine, closed

    @property
    def tracker(self):
        """stage_steps() plus an icon and a date for each box: [{label, state, icon, date, note}]."""
        steps = self.stage_steps()
        first = next((n for n in self.notices.all() if n.kind == "violation"), None)
        legal_ev = self.events.filter(text__icontains="attorney").order_by("-created_at").first()
        dates = {
            0: (first.created_at.date() if first else self.created_at.date(), "Issued"),
            1: (self.cure_deadline, "Cure by"),
            2: (self.board_meeting_date, "Voted"),
            3: (self.hearing_date.date() if self.hearing_date else None, "Hearing"),
            4: (self.ledger_posted_on or self.suspension_effective_on, "Imposed"),
            5: (self.cured_on or self.fine_paid_on, "Closed"),
        }
        out, idx = [], 0
        for label, state in steps:
            if state == "legal":
                out.append({"label": label, "state": state, "icon": "\u2696",
                            "date": (self.legal_sent_on.date() if self.legal_sent_on else self.legal_referred_on),
                            "note": "Sent to attorney" if self.legal_sent_on else "Referred, email not sent"})
                continue
            d, note = dates[idx]
            icon = "\u21b7" if state == "skipped" else self.ICONS[idx]
            out.append({"label": label, "state": state, "icon": icon, "date": d if state != "skipped" else None, "note": note})
            idx += 1
        return out

    @property
    def needs_reinspection(self):
        return bool(self.reinspect_on and self.status in self.OPEN and self.status != self.Status.DRAFT)

    @property
    def reinspect_age(self):
        return (date.today() - self.reinspect_on).days if self.reinspect_on else None

    def get_status_display(self):
        if self.needs_reinspection:
            return "Re-inspect"
        if self.status == self.Status.NOTICE_SENT and self.off_hold_on:
            return "Off hold: cure period restarted"
        return self._get_FIELD_display(self._meta.get_field("status"))

    @property
    def stage_flag(self):
        if self.status == self.Status.ON_HOLD:
            return "On hold" + (f" until {short_date(self.hold_until)}" if self.hold_until else "")
        if self.status == self.Status.NOTICE_SENT and self.off_hold_on:
            return "Off hold"
        if self.status == self.Status.REFERRED_LEGAL:
            return "Sent to legal"
        return ""

    @property
    def non_owner_occupied(self):
        """Rented / occupied by someone other than the owner: the tenant is the addressee and the owner gets a copy."""
        return self.owner.has_tenant

    @property
    def pending_extension(self):
        """The owner's oldest unanswered request for more time, if any."""
        return self.responses.filter(extension_requested=True, handled=False).first()

    @property
    def decision_letter_due(self):
        """The written-findings letter must go out within DECISION_LETTER_DAYS of the hearing."""
        from datetime import timedelta
        return (self.hearing_date.date() + timedelta(days=settings.DECISION_LETTER_DAYS)) if self.hearing_date else None

    @property
    def is_immediate(self):
        return self.non_curable or self.safety_issue or self.board_emergency

    @property
    def cure_phrase(self):
        """'immediately' or 'within N days' (used in letters and screens)."""
        n = self.total_cure_days
        return "immediately" if n == 0 else f"within {n} days"

    @property
    def total_cure_days(self):
        return self.cure_days + self.extended_days

    def get_absolute_url(self):
        return reverse("violation_detail", args=[self.pk])

    def resident_url(self):
        return f"{settings.SITE_URL}{reverse('resident', args=[self.token])}"

    def fines_total_for_owner_same_type(self):
        """Aggregate approved fines for this owner/type, to check the continuing-violation cap."""
        qs = Violation.objects.filter(owner=self.owner, vtype=self.vtype,
                                      status=self.Status.FINE_APPROVED).exclude(pk=self.pk)
        return qs.aggregate(t=models.Sum("fine_amount"))["t"] or 0


class Photo(models.Model):
    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to=photo_path)
    caption = models.CharField(max_length=200, blank=True)
    from_resident = models.BooleanField(default=False, help_text="Cure evidence uploaded by the owner.")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    taken_at = models.DateTimeField(null=True, blank=True, help_text="From the photo's EXIF data when present.")

    @property
    def shown_at(self):
        return self.taken_at or self.uploaded_at


class Notice(models.Model):
    class Kind(models.TextChoices):
        VIOLATION = "violation", "Notice of violation"
        EXTENSION = "extension", "Extension granted"
        HEARING = "hearing", "Notice of committee hearing"
        SECOND = "second", "Second notice of violation"
        FINE = "fine", "Notice of committee decision (written findings)"

    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="notices")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    emailed_to = models.EmailField(blank=True)
    mailed_on = models.DateField(null=True, blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)
    method = models.CharField(max_length=20, blank=True, choices=[("first_class", "First-class mail"),
        ("certified", "Certified, return receipt"), ("hand", "Hand delivered")])
    return_receipt_on = models.DateField(null=True, blank=True)
    provider = models.CharField(max_length=20, blank=True, help_text="Print/mail provider that handled it.")
    provider_ids = models.CharField(max_length=500, blank=True)
    provider_note = models.CharField(max_length=300, blank=True)
    email_error = models.CharField(max_length=300, blank=True, help_text="Set if the email could not be sent.")
    mail_hold_until = models.DateField(null=True, blank=True, help_text="Email went out; the printed copy waits until this date.")
    mail_cancelled = models.BooleanField(default=False, help_text="The held mailing was dropped (corrected before the mail date).")
    mail_tried_on = models.DateField(null=True, blank=True, help_text="Last day the app tried to hand this to the mail provider.")

    @property
    def mail_held(self):
        return bool(self.mail_hold_until and not self.mailed_on and not self.mail_cancelled)

    class Meta:
        ordering = ["created_at"]


    def delivery_lines(self, staff=False):
        """Plain-language delivery record: when it was emailed, mailed (method, tracking number), return receipt."""
        out = []
        if self.emailed_at:
            out.append(f"Emailed {short_date(self.emailed_at)} to {self.emailed_to}")
        elif self.email_error:
            out.append("Email could not be delivered")
        if self.mail_held:
            out.append(f"US Mail copy scheduled for {short_date(self.mail_hold_until)}")
        elif self.mail_cancelled:
            out.append("US Mail copy not sent (the violation was corrected first)")
        if self.mailed_on:
            how = self.get_method_display() or "Mailed"
            out.append(f"{how} by US Mail {short_date(self.mailed_on)}" + (f", tracking number {self.tracking_number}" if self.tracking_number else ""))
            if self.return_receipt_on:
                out.append(f"Return receipt received {short_date(self.return_receipt_on)}")
        if staff and self.provider:
            out.append(f"Print/mail provider: {self.provider} {self.provider_ids}".strip() + (f" ({self.provider_note})" if self.provider_note else ""))
        return out


class EmailLog(models.Model):
    """A copy of every email the app sends about a violation, so the board and manager can see exactly what the owner was told."""
    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="emails")
    notice = models.ForeignKey("Notice", null=True, blank=True, on_delete=models.SET_NULL, related_name="email_logs")
    subject = models.CharField(max_length=250)
    body = models.TextField()
    to = models.CharField(max_length=500)
    recipient = models.CharField(max_length=10, blank=True, help_text="owner or tenant")
    sent_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField(default=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["sent_at"]


class Response(models.Model):
    """Messages between the owner and the association (owner replies, extension requests, staff replies)."""
    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="responses")
    from_resident = models.BooleanField(default=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    message = models.TextField()
    extension_requested = models.BooleanField(default=False)
    extension_days = models.PositiveIntegerField(null=True, blank=True)
    cure_reported = models.BooleanField(default=False, help_text="Owner says it is fixed; needs reinspection.")
    attorney_notice = models.BooleanField(default=False, help_text="Owner will bring an attorney to the hearing.")
    handled = models.BooleanField(default=False, help_text="Staff has dealt with this request.")
    decision = models.CharField(max_length=10, blank=True, choices=[("approved", "Approved"), ("declined", "Declined")])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Reminder(models.Model):
    """One cure-deadline reminder per case, deadline and threshold, so the daily job never double-sends."""
    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="reminders")
    deadline = models.DateField()
    days_before = models.PositiveSmallIntegerField()
    sent_at = models.DateTimeField(null=True, blank=True, help_text="Empty if skipped because a later reminder went out instead.")

    class Meta:
        unique_together = ("violation", "deadline", "days_before")


class Event(models.Model):
    """Audit trail / timeline shown on each violation."""
    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="events")
    text = models.CharField(max_length=500)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Profile(models.Model):
    """Role for a login. Superusers/staff without a profile are treated as managers."""
    class Role(models.TextChoices):
        MANAGER = "manager", "Manager (full access)"
        BOARD = "board", "Board member (sees everything)"
        COMMITTEE = "committee", "Compliance Committee (assigned hearings only)"
        HOMEOWNER = "homeowner", "Homeowner (own violations)"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.HOMEOWNER)
    owner = models.ForeignKey(Owner, null=True, blank=True, on_delete=models.SET_NULL, related_name="profiles",
                              help_text="Required for homeowner logins. Everyone linked to the same owner record sees the same case data.")
    relationship = models.CharField(max_length=10, default="owner", choices=[
        ("owner", "Owner"), ("co_owner", "Co-owner"), ("tenant", "Tenant / renter"), ("other", "Other household member")])

    def __str__(self):
        return f"{self.user} ({self.role})"


class ImportMapping(models.Model):
    """Remembers how a file's columns were matched, so the next export with the same headers needs no re-matching."""
    signature = models.CharField(max_length=500, unique=True)
    mapping = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class CannedReply(models.Model):
    """A saved reply the board and manager can insert when answering an owner. Everyone on the board/management team shares them."""
    title = models.CharField(max_length=80)
    body = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title
