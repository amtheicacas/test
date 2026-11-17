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
    tenant_name = models.CharField(max_length=200, blank=True, help_text="If the home is rented, notices also go to the tenant.")
    tenant_email = models.EmailField(blank=True)
    tenant_phone = models.CharField(max_length=50, blank=True)
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

    @property
    def tenant_mail_block(self):
        return f"{self.tenant_name}\n{self.property_address}"


class ViolationType(models.Model):
    name = models.CharField(max_length=200)
    reference = models.CharField(max_length=200, blank=True,
                                 help_text="Governing document cite, e.g. 'Declaration Art. VII §3(b)'.")
    default_cure_days = models.PositiveIntegerField(default=15)
    default_fine = models.DecimalField(max_digits=8, decimal_places=2, default=100)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Violation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        NOTICE_SENT = "notice_sent", "Cure period running"
        PENDING_BOARD = "pending_board", "Awaiting board vote"
        HEARING_NEEDED = "hearing_needed", "Board approved — schedule hearing"
        HEARING_SCHEDULED = "hearing_scheduled", "Committee hearing scheduled"
        FINE_APPROVED = "fine_approved", "Imposed by committee — awaiting payment / cure"
        FINE_REJECTED = "fine_rejected", "Rejected by committee"
        REFERRED_LEGAL = "referred_legal", "Referred to attorney"
        CLOSED = "closed", "Closed (cured and paid)"
        CURED = "cured", "Cured"
        DISMISSED = "dismissed", "Dismissed"

    OPEN = [Status.DRAFT, Status.NOTICE_SENT, Status.PENDING_BOARD, Status.HEARING_NEEDED,
            Status.HEARING_SCHEDULED, Status.FINE_APPROVED]

    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name="violations")
    vtype = models.ForeignKey(ViolationType, on_delete=models.PROTECT, verbose_name="violation type")
    observed_on = models.DateField(default=date.today)
    description = models.TextField(help_text="What was observed.")
    cure_required = models.TextField(help_text="What the owner must do to cure.")
    cure_days = models.PositiveIntegerField(default=15, help_text="Days allowed to cure from the notice date.")
    cure_deadline = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    token = models.CharField(max_length=64, unique=True, editable=False, default=secrets.token_urlsafe)

    safety_issue = models.BooleanField(default=False, help_text="Threatens health/safety — Board may act before the cure deadline.")
    # Board stage: the vote only PROPOSES a fine and/or suspension
    board_meeting_date = models.DateField(null=True, blank=True)
    proposed_fine = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    proposed_suspension = models.CharField(max_length=300, blank=True, help_text="e.g. 'Pool and clubhouse use for 30 days'.")
    # Committee stage
    hearing_date = models.DateTimeField(null=True, blank=True)
    hearing_location = models.CharField(max_length=250, blank=True)
    committee_votes_for = models.PositiveSmallIntegerField(null=True, blank=True)
    committee_votes_against = models.PositiveSmallIntegerField(null=True, blank=True)
    fine_amount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="Imposed amount (set when committee approves).")
    suspension = models.CharField(max_length=300, blank=True)
    fine_due_on = models.DateField(null=True, blank=True)
    fine_paid_on = models.DateField(null=True, blank=True)
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Event(models.Model):
    """Audit trail / timeline shown on each violation."""
    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name="events")
    text = models.CharField(max_length=500)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
