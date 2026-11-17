import secrets
from datetime import date

from django.conf import settings
from django.db import models
from django.urls import reverse


def photo_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"photos/{secrets.token_hex(16)}.{ext}"


class Owner(models.Model):
    name = models.CharField(max_length=200)
    property_address = models.CharField(max_length=250, unique=True)
    email = models.EmailField(blank=True, help_text="Leave blank to send by mail only.")
    phone = models.CharField(max_length=50, blank=True)
    mailing_address = models.TextField(blank=True, help_text="If different from the property address.")
    notes = models.TextField(blank=True)
    city = models.CharField(max_length=80, default="Sarasota")
    state = models.CharField(max_length=2, default="FL")
    zip_code = models.CharField(max_length=10, blank=True)
    mail_line1 = models.CharField("mailing street", max_length=200, blank=True, help_text="Only if different from the property.")
    mail_city = models.CharField(max_length=80, blank=True)
    mail_state = models.CharField(max_length=2, blank=True)
    mail_zip = models.CharField(max_length=10, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    co_owner_email = models.EmailField(blank=True, help_text="A second owner on the deed. Gets notices and can have their own login.")
    tenant_name = models.CharField(max_length=200, blank=True, help_text="If the home is rented, notices also go to the tenant.")
    tenant_email = models.EmailField(blank=True)
    tenant_phone = models.CharField(max_length=50, blank=True)
    active = models.BooleanField(default=True, help_text="Untick when the owner has left (the sync does this). Disables their login.")
    external_id = models.CharField(max_length=100, unique=True, null=True, blank=True,
                                   help_text="Account/homeowner ID in Vantaca (set by sync).")
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["property_address"]

    def __str__(self):
        return f"{self.name} — {self.property_address}"

    @property
    def mail_block(self):
        return f"{self.name}\n{self.mailing_address.strip() or self.property_address}"

    def postal(self, tenant=False):
        """Structured address for the print/mail provider."""
        if tenant:
            return {"name": self.tenant_name, "line1": self.property_address, "city": self.city,
                    "state": self.state, "zip": self.zip_code}
        if self.mail_line1:
            return {"name": self.name, "line1": self.mail_line1, "city": self.mail_city or self.city,
                    "state": self.mail_state or self.state, "zip": self.mail_zip or self.zip_code}
        return {"name": self.name, "line1": self.property_address, "city": self.city,
                "state": self.state, "zip": self.zip_code}

    @property
    def tenant_mail_block(self):
        return f"{self.tenant_name}\n{self.property_address}"


class ViolationType(models.Model):
    name = models.CharField(max_length=200)
    reference = models.CharField(max_length=200, blank=True,
                                 help_text="Governing document cite, e.g. 'Declaration Art. VII §3(b)'.")
    default_cure_days = models.PositiveIntegerField(default=15)
    default_fine = models.DecimalField(max_digits=8, decimal_places=2, default=100)
    # Filled from the Community Standards (load_standards); blank for hand-made types
    code = models.CharField(max_length=30, unique=True, null=True, blank=True, help_text="e.g. 3.22-timing")
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
        LEDGER_PENDING = "ledger_pending", "Committee approved — awaiting approval to post to ledger"
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
    cure_days = models.PositiveIntegerField(default=15, help_text="Days allowed to cure from the notice date.")
    cure_deadline = models.DateField(null=True, blank=True)
    extended_days = models.PositiveIntegerField(default=0, help_text="Total days added to the cure period by approved extensions.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    token = models.CharField(max_length=64, unique=True, editable=False, default=secrets.token_urlsafe)

    safety_issue = models.BooleanField(default=False, help_text="Threatens health/safety — Board may act before the cure deadline.")
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
    # Committee stage
    committee_meeting = models.ForeignKey(CommitteeMeeting, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="violations")
    hearing_date = models.DateTimeField(null=True, blank=True)
    hearing_location = models.CharField(max_length=250, blank=True)
    committee_votes_for = models.PositiveSmallIntegerField(null=True, blank=True)
    committee_votes_against = models.PositiveSmallIntegerField(null=True, blank=True)
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
        """[(label, 'done'|'current'|'todo')] for the progress tracker shown to owners and staff."""
        cur = self.stage_index()
        return [(label, "done" if i < cur or (cur == 5 and i == 5) else "current" if i == cur else "todo")
                for i, label in enumerate(self.STEPS)]

    @property
    def stage_flag(self):
        if self.status == self.Status.ON_HOLD:
            return "On hold"
        if self.status == self.Status.REFERRED_LEGAL:
            return "Sent to legal"
        return ""

    @property
    def non_owner_occupied(self):
        """Rented / occupied by someone other than the owner: the tenant is the addressee and the owner gets a copy."""
        return bool(self.owner.tenant_name)

    @property
    def pending_extension(self):
        """The owner's oldest unanswered request for more time, if any."""
        return self.responses.filter(extension_requested=True, handled=False).first()

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
        FINE = "fine", "Notice of imposition of fine / suspension"

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

    class Meta:
        ordering = ["created_at"]


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
