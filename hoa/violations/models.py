import hashlib
import re
import secrets
from datetime import date


def short_date(d):
    """'Nov 5, 2026' (the '%-d' strftime flag does not work on Windows)."""
    return f"{d:%b} {d.day}, {d.year}"

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone


def photo_path(instance, filename):
    from .uploads import safe_ext        # the name the uploader typed is never trusted: only picture extensions are kept
    return f"photos/{secrets.token_hex(16)}.{safe_ext(filename)}"


def sent_letter_path(instance, filename):
    return f"sent-letters/{timezone.now():%Y}/{secrets.token_hex(16)}.pdf"      # random name: never guessable, never the owner's name


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
    property_address = models.CharField(max_length=250, help_text="The physical address. Default mailing address. Only one ACTIVE owner can have an address; former owners keep theirs.")
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
    inactive_since = models.DateField(null=True, blank=True, help_text="When the owner was marked inactive (moved out or sold). Kept for the retention period.")
    inactive_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["property_address"]
        constraints = [models.UniqueConstraint(fields=["property_address"], condition=models.Q(active=True), name="one_active_owner_per_address")]

    def __str__(self):
        return f"{self.name} — {self.property_address}"

    def save(self, *args, **kwargs):
        if not self.active and self.inactive_since is None:
            self.inactive_since = date.today()
        elif self.active and self.inactive_since is not None:
            self.inactive_since, self.inactive_reason = None, ""
        super().save(*args, **kwargs)

    @property
    def keep_until(self):
        """Inactive records are kept this long (ARCHIVE_RETENTION_YEARS) before they may be purged."""
        if self.inactive_since is None:
            return None
        try:
            return self.inactive_since.replace(year=self.inactive_since.year + settings.ARCHIVE_RETENTION_YEARS)
        except ValueError:                       # Feb 29
            return self.inactive_since.replace(year=self.inactive_since.year + settings.ARCHIVE_RETENTION_YEARS, day=28)

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
    zoom_api_id = models.CharField(max_length=40, blank=True, help_text="Set when the app created the Zoom meeting itself.")
    series = models.CharField(max_length=60, blank=True, help_text="Set when it was created as part of a repeating series, e.g. '3rd Monday of each month'.")
    location = models.CharField(max_length=250, blank=True, help_text="In-person place, if any.")
    notes = models.TextField(blank=True)
    attendees = models.TextField(blank=True, help_text="Other committee members present who are not on the year's roster.")
    members_absent = models.TextField(blank=True)
    guests = models.TextField(blank=True, help_text="Others present (owners, representatives, management).")
    called_to_order = models.TimeField(null=True, blank=True)
    adjourned = models.TimeField(null=True, blank=True)
    minutes = models.FileField(upload_to="minutes/", blank=True, help_text="Optional: minutes uploaded as a file. The app also writes minutes from the entries.")

    class Minutes(models.TextChoices):
        DRAFT = "draft", "Draft: awaiting approval"
        APPROVED = "approved", "Approved"

    minutes_status = models.CharField(max_length=10, choices=Minutes.choices, default=Minutes.DRAFT)
    minutes_approved_on = models.DateField(null=True, blank=True)
    minutes_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    minutes_approved_at = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="minutes_approved_here",
                                            help_text="The later meeting at which these minutes were approved.")

    class Results(models.TextChoices):
        OPEN = "open", "Committee working"
        SUBMITTED = "submitted", "Awaiting administrative review"
        PROCESSED = "processed", "Reviewed and processed"

    results_status = models.CharField(max_length=10, choices=Results.choices, default=Results.OPEN)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return f"Committee meeting {self.starts_at:%b %d, %Y %I:%M %p}"

    def roster(self):
        """This year's committee members. If none are loaded for the meeting's year, the most recent roster before it."""
        from django.utils import timezone
        year = timezone.localtime(self.starts_at).year
        qs = CommitteeMember.objects.filter(active=True, year=year)
        if not qs.exists():
            last = CommitteeMember.objects.filter(active=True, year__lt=year).order_by("-year").values_list("year", flat=True).first()
            qs = CommitteeMember.objects.filter(active=True, year=last) if last else CommitteeMember.objects.none()
        return list(qs.order_by("name"))

    @property
    def place(self):
        return " · ".join(x for x in [f"Zoom: {self.zoom_link}" if self.zoom_link else "", self.location] if x) or "to be announced"

    @property
    def has_occurred(self):
        from django.utils import timezone
        return self.starts_at <= timezone.now()


class CommitteeMember(models.Model):
    """A Compliance Committee member for a year. The roster is loaded once a year; the meeting page then just ticks who attended."""
    class Role(models.TextChoices):
        CHAIR = "chair", "Chair"
        SECRETARY = "secretary", "Secretary"
        MEMBER = "member", "Member"

    name = models.CharField(max_length=120)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    year = models.PositiveSmallIntegerField(help_text="The year of service this roster entry covers.")
    active = models.BooleanField(default=True, help_text="Untick if the member left mid-year.")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                             help_text="Their login. It switches off by itself when they are not on the current year's roster.")
    owner = models.ForeignKey("Owner", null=True, blank=True, on_delete=models.SET_NULL, related_name="committee_seats",
                              help_text="Picked from the owner list (which comes from Vantaca), so they sign in once with their owner login.")

    class Meta:
        ordering = ["year", "name"]

    def __str__(self):
        return f"{self.name} ({self.year})"


class MeetingAttendance(models.Model):
    meeting = models.ForeignKey("CommitteeMeeting", on_delete=models.CASCADE, related_name="attendance")
    member = models.ForeignKey(CommitteeMember, on_delete=models.CASCADE, related_name="attendance")
    status = models.CharField(max_length=8, choices=[("present", "Present"), ("absent", "Absent"), ("excused", "Absent (excused)")])

    class Meta:
        unique_together = ("meeting", "member")


class Violation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        NOTICE_SENT = "notice_sent", "Cure period running"
        PENDING_BOARD = "pending_board", "Awaiting board vote"
        HEARING_NEEDED = "hearing_needed", "Board approved — schedule hearing"
        HEARING_SCHEDULED = "hearing_scheduled", "Sent to Compliance Committee"
        ON_HOLD = "on_hold", "On hold"
        LEDGER_PENDING = "ledger_pending", "Committee decided: administrative review"
        WAIVED = "waived", "Waived by the Committee (closed)"
        FINE_APPROVED = "fine_approved", "Fine imposed — awaiting payment / cure"
        FINE_REJECTED = "fine_rejected", "Rejected by committee"
        REFERRED_LEGAL = "referred_legal", "Sent to legal"
        CLOSED = "closed", "Closed (cured and paid)"
        CURED = "cured", "Cured"
        DISMISSED = "dismissed", "Dismissed"

    OPEN = [Status.DRAFT, Status.NOTICE_SENT, Status.PENDING_BOARD, Status.HEARING_NEEDED,
            Status.HEARING_SCHEDULED, Status.LEDGER_PENDING,
            Status.FINE_APPROVED, Status.ON_HOLD, Status.REFERRED_LEGAL]

    # What the owner portal groups under "Closed": everything that has run its course, including a fine that has been imposed.
    OWNER_CLOSED = [Status.CURED, Status.CLOSED, Status.DISMISSED, Status.FINE_REJECTED, Status.WAIVED, Status.FINE_APPROVED]

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
    committee_owner_present = models.BooleanField(default=False, help_text="The owner or a representative attended the hearing.")
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

    @property
    def progress(self):
        """The owner's progress tracker (the Association's six steps with a date and, for the current one, a plain sentence)."""
        from . import progress
        return progress.stages(self)

    @property
    def staff_progress(self):
        """The same steps with sub-steps, for the Board / Manager."""
        from . import progress
        return progress.staff_stages(self)

    @property
    def owner_closed(self):
        return self.status in self.OWNER_CLOSED

    # What the owner portal calls each status: short, plain and consistent. (label, color)
    PORTAL_LABELS = {
        "notice_sent": ("In Cure Period", "yellow"),
        "pending_board": ("Sent to Board for Fine", "orange"),
        "hearing_needed": ("Hearing Being Scheduled", "orange"),
        "hearing_scheduled": ("Sent to Compliance Committee", "red"),
        "ledger_pending": ("Decision Being Processed", "red"),
        "fine_approved": ("Fine Imposed", "red"),
        "fine_rejected": ("Closed: No Fine", ""),
        "waived": ("Closed: Waived", ""),
        "cured": ("Closed: Corrected", ""),
        "closed": ("Closed", ""),
        "dismissed": ("Closed: Dismissed", ""),
        "on_hold": ("On Hold", ""),
        "referred_legal": ("Sent to Legal", "red"),
    }

    @property
    def portal_status(self):
        """(label, color) for the status pill the owner sees."""
        if self.needs_reinspection:
            return ("Verifying Your Correction", "yellow")
        if self.status == self.Status.REFERRED_LEGAL and self.legal_path == "after_fine":
            return ("Fine Imposed, Sent to Legal", "red")
        return self.PORTAL_LABELS.get(self.status, (self.get_status_display(), ""))

    @property
    def needs_reinspection(self):
        return bool(self.reinspect_on and self.status in self.OPEN and self.status != self.Status.DRAFT)

    @property
    def reinspect_age(self):
        return (date.today() - self.reinspect_on).days if self.reinspect_on else None

    def get_status_display(self):
        if self.status == self.Status.REFERRED_LEGAL and self.legal_path == "after_fine":
            return "Fine imposed — sent to legal"
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
    def is_point_in_time(self):
        """A one-time event that can't be cured (dog off leash, parking in the street...). Goes straight to the Board queue."""
        return bool(self.non_curable)

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
            pieces = list(self.pieces.all())
            out.append(f"{how} by US Mail {short_date(self.mailed_on)}" + (f", tracking number {self.tracking_number}" if self.tracking_number and not pieces else ""))
            out += [p.summary for p in pieces if p.is_certified or p.tracking_number]
            if self.return_receipt_on:
                out.append(f"Return receipt received {short_date(self.return_receipt_on)}")
        if staff and self.provider:
            out.append(f"Print/mail provider: {self.provider} {self.provider_ids}".strip() + (f" ({self.provider_note})" if self.provider_note else ""))
        return out


class MailPiece(models.Model):
    """One physical letter handed to the print/mail provider (a notice can be several: owner mailing address, property address, tenant;
    certified and/or first class). Certified pieces carry a USPS tracking number whose progress is pulled in from the provider."""
    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="pieces")
    provider_id = models.CharField(max_length=80, blank=True)
    label = models.CharField(max_length=60, blank=True, help_text="Who it was addressed to, e.g. owner mailing address.")
    service = models.CharField(max_length=12, default="first_class", help_text="certified or first_class")
    tracking_number = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=80, blank=True, help_text="Latest tracking event, e.g. In Transit, Delivered.")
    events = models.JSONField(default=list, blank=True)
    expected_delivery_on = models.DateField(null=True, blank=True)
    delivered_on = models.DateField(null=True, blank=True)
    problem = models.BooleanField(default=False, help_text="Returned, re-routed or otherwise not delivered as planned.")
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    @property
    def is_certified(self):
        return self.service == "certified"

    @property
    def usps_url(self):
        return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={self.tracking_number}" if self.tracking_number else ""

    @property
    def summary(self):
        kind = "Certified" if self.is_certified else "First class"
        out = f"{kind} to {self.label}" if self.label else kind
        if self.tracking_number:
            out += f", tracking number {self.tracking_number}"
        if self.delivered_on:
            out += f": delivered {short_date(self.delivered_on)}"
        elif self.status:
            out += f": {self.status}"
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


class SentLetter(models.Model):
    """The EXACT PDF of a letter at the moment it was emailed or handed to the mail service, kept for the case file. Add-only: a saved copy
    can never be edited or deleted through the application (no page offers it, and the admin shows it read-only). If the owner and the
    tenant each get their own letter, each is saved; a letter that goes by email and by mail is saved for each channel."""

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        FIRST_CLASS = "first_class", "First-class mail"
        CERTIFIED = "certified", "Certified mail, return receipt"

    class Origin(models.TextChoices):
        SENT = "sent", "Exact copy saved when it was sent"
        UPLOADED = "uploaded", "Original PDF uploaded by staff"
        REBUILT = "rebuilt", "Rebuilt copy (not the exact original)"

    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="sent_letters")
    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="sent_copies")
    kind = models.CharField(max_length=20, help_text="Which letter (the notice's kind when it was sent).")
    recipient = models.CharField(max_length=20, help_text="Which version: owner, owner_property (property address) or tenant.")
    recipient_name = models.CharField(max_length=200, blank=True)
    sent_to = models.TextField(blank=True, help_text="The email addresses, or the mailing address, it went to.")
    channel = models.CharField(max_length=20, choices=Channel.choices)
    tracking_number = models.CharField(max_length=100, blank=True, help_text="Certified-mail tracking number, if known when it was sent.")
    mail_piece = models.ForeignKey("MailPiece", null=True, blank=True, on_delete=models.SET_NULL, related_name="saved_copies")
    sent_at = models.DateTimeField(default=timezone.now, editable=False)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                help_text="Blank when the application sent it by itself (for example a held mailing released by the daily job).")
    file = models.FileField(upload_to=sent_letter_path)
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveIntegerField(default=0)
    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.SENT)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["sent_at", "pk"]

    def __str__(self):
        return f"Case #{self.violation_id} {self.get_kind_display()} ({self.get_channel_display()}) {self.sent_at:%Y-%m-%d}"

    def get_kind_display(self):
        return dict(Notice.Kind.choices).get(self.kind, self.kind)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A saved copy of a letter can never be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("A saved copy of a letter can never be deleted.")

    def read_bytes(self):
        with self.file.open("rb") as f:
            return f.read()

    def intact(self):
        """True if the stored file still matches the fingerprint taken when it was saved."""
        try:
            return hashlib.sha256(self.read_bytes()).hexdigest() == self.sha256
        except Exception:       # noqa: BLE001  (file missing or storage unreachable)
            return False


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
    read_at = models.DateTimeField(null=True, blank=True, help_text="When a board member or the manager first opened the case after this owner message.")
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
    text = models.TextField()      # no length limit: notes typed by staff are quoted here, and PostgreSQL (unlike SQLite) enforces a limit
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Profile(models.Model):
    """Role for a login. Superusers/staff without a profile are treated as managers."""
    class Role(models.TextChoices):
        MANAGER = "manager", "Manager (full access, same as Board)"
        BOARD = "board", "Board member (full access, same as Manager)"
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

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Managers and board members have the same access, including the admin screens (turn off with STAFF_ADMIN_ACCESS=0).
        if self.role in (self.Role.MANAGER, self.Role.BOARD) and settings.STAFF_ADMIN_ACCESS and not (self.user.is_staff and self.user.is_superuser):
            type(self.user).objects.filter(pk=self.user_id).update(is_staff=True, is_superuser=True)


class ImportMapping(models.Model):
    """Remembers how a file's columns were matched, so the next export with the same headers needs no re-matching."""
    signature = models.CharField(max_length=500, unique=True)
    mapping = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class LoginLink(models.Model):
    """A one-time sign-in link emailed to an owner. Only a hash of the token is stored; it is good for a few minutes and works once."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_links")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)


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


class CaseAbstention(models.Model):
    """A committee member who did not vote on a case: automatically when it is their own violation, or by choice (a conflict)."""
    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="abstentions")
    member = models.ForeignKey(CommitteeMember, on_delete=models.CASCADE, related_name="abstentions")
    reason = models.CharField(max_length=120, default="Abstained")

    class Meta:
        unique_together = ("violation", "member")
