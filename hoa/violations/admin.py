from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from django.conf import settings

from . import accounts, audit
from .models import AuditLog, CannedReply, CommitteeMeeting, CommitteeMember, MinutesFile, Owner, Profile, SentLetter, SiteVisitAccess, SiteVisitObservation, SiteVisitPhoto, Violation, ViolationType


SECRET_ADMIN_FIELDS = ("password", "last_login")


class SafeAdmin(admin.ModelAdmin):
    """Records that matter are not deleted from the admin on a LIVE site (deleting one also deletes its history, letters and photos);
    deleting is only for clearing test data on a development site. Every change made here is written to the audit log (old -> new,
    never a password)."""
    audit_fields = ()           # field names whose old -> new values are written to the audit log (empty = all changed fields)

    def has_delete_permission(self, request, obj=None):
        return settings.DEBUG and super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and form.changed_data:
            fields = [f for f in form.changed_data if f not in SECRET_ADMIN_FIELDS] or []
            old = {f: form.initial.get(f, "") for f in fields}
            new = {f: form.cleaned_data.get(f, "") for f in fields}
            note = audit.diff(old, new, fields) if fields else ""
            if "password" in form.changed_data:
                note = (note + "; " if note else "") + "password changed"
            if note:
                audit.record(f"admin.{obj._meta.model_name}.changed", str(obj), note, request=request)
        elif not change:
            audit.record(f"admin.{obj._meta.model_name}.created", str(obj), "", request=request)


@admin.action(description=f"Send {settings.APP_NAME} invitation (creates homeowner login)")
def invite(modeladmin, request, queryset):
    sent = sum(1 for o in queryset if accounts.invite_owner(o, request))
    messages.success(request, f"{sent} invitation(s) sent. Owners without an email or already invited were skipped.")


@admin.action(description="Mark as moved out / sold (inactive, kept for the retention period)")
def mark_inactive(modeladmin, request, queryset):
    n = 0
    for o in queryset.filter(active=True):
        o.active, o.inactive_reason = False, "Marked inactive by " + request.user.get_username()
        o.save()
        accounts.sync_account(o)
        audit.record("owner.marked_inactive", o.property_address, f"{o.name}: {o.inactive_reason}", request=request)
        n += 1
    messages.success(request, f"{n} owner(s) marked inactive. Find them under Former owners.")


@admin.register(Owner)
class OwnerAdmin(SafeAdmin):
    search_fields = ["name", "property_address", "email"]
    list_display = ["name", "property_address", "email", "tenant_name", "active", "inactive_since", "last_synced_at"]
    list_filter = ["active"]
    actions = [invite, mark_inactive]
    # An email address decides WHO can sign in to a household's records, so it is not edited here (a typo would hand a stranger the records).
    # Use the "Logins" page on the owner (Change sign-in email) or fix it in Vantaca.
    LOGIN_FIELDS = ("email", "additional_emails", "co_owner_email", "tenant_email", "tenant_additional_emails")

    def get_readonly_fields(self, request, obj=None):
        return (*super().get_readonly_fields(request, obj), *(self.LOGIN_FIELDS if obj is not None else ()))


@admin.register(ViolationType)
class ViolationTypeAdmin(SafeAdmin):
    list_display = ["name", "reference", "default_cure_days", "default_fine"]


@admin.register(CommitteeMeeting)
class CommitteeMeetingAdmin(SafeAdmin):
    list_display = ["starts_at", "zoom_link"]


@admin.register(CommitteeMember)
class CommitteeMemberAdmin(SafeAdmin):
    list_display = ["name", "role", "year", "active"]
    list_filter = ["year"]


@admin.register(CannedReply)
class CannedReplyAdmin(SafeAdmin):
    list_display = ["title", "created_by", "created_at"]


@admin.register(Violation)
class ViolationAdmin(SafeAdmin):
    """For cleaning up test data. Deleting a violation also deletes its letters, emails, messages, photo records and history."""
    list_display = ["id", "owner", "vtype", "status", "observed_on", "created_at"]
    list_filter = ["status"]
    search_fields = ["owner__name", "owner__property_address", "vtype__name", "description"]
    list_select_related = ["owner", "vtype"]
    date_hierarchy = "created_at"


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False


User = get_user_model()
admin.site.unregister(User)


@admin.register(User)
class UserAdminWithRole(SafeAdmin, UserAdmin):
    """Logins. Roles and access changes made here are audited. Logins are switched off (not deleted) when someone leaves."""
    inlines = [ProfileInline]
    list_display = ["username", "email", "first_name", "last_name", "is_staff", "is_active"]

    def save_formset(self, request, form, formset, change):
        before = {f.instance.pk: (f.instance.role, f.instance.owner_id, f.instance.relationship) for f in formset.forms if f.instance.pk}
        super().save_formset(request, form, formset, change)
        for f in formset.forms:
            if f.instance.pk and before.get(f.instance.pk) not in (None, (f.instance.role, f.instance.owner_id, f.instance.relationship)):
                old = before[f.instance.pk]
                audit.record("admin.profile.changed", str(form.instance),
                             f"role: {old[0]} -> {f.instance.role}; owner id: {old[1]} -> {f.instance.owner_id}; relationship: {old[2]} -> {f.instance.relationship}",
                             request=request)


from django_otp.plugins.otp_totp.models import TOTPDevice      # noqa: E402

admin.site.unregister(TOTPDevice)


@admin.register(TOTPDevice)
class TOTPDeviceLookOnly(admin.ModelAdmin):
    """Two-step devices are look-only here: the secret key is never shown, and a lost phone is handled by the audited 'Reset two-step' action
    on the Staff logins page (a different Board member, with a reason), not by deleting a row."""
    exclude = ["key"]
    list_display = ["user", "name", "confirmed"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """The account and record audit trail: look only."""
    list_display = ["at", "actor_name", "actor_role", "action", "subject", "ip_address"]
    list_filter = ["action", "actor_role"]
    search_fields = ["actor_name", "subject", "detail", "action"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SentLetter)
class SentLetterAdmin(admin.ModelAdmin):
    """The saved exact copies of letters sent: look only. Nobody can add, change or delete one here."""
    list_display = ["violation", "kind", "recipient_name", "channel", "sent_at", "tracking_number", "sha256"]
    list_filter = ["kind", "channel", "origin"]
    search_fields = ["violation__owner__property_address", "recipient_name", "tracking_number", "sha256"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class _LookOnly(admin.ModelAdmin):
    """Saved evidence: look only. Nobody can add, change or delete a row here."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MinutesFile)
class MinutesFileAdmin(_LookOnly):
    list_display = ["meeting", "original_name", "kind", "uploaded_by_name", "uploaded_at", "size", "sha256"]


@admin.register(SiteVisitObservation)
class SiteVisitObservationAdmin(_LookOnly):
    list_display = ["violation", "author_name", "author_role", "created_at", "corrects"]


@admin.register(SiteVisitPhoto)
class SiteVisitPhotoAdmin(_LookOnly):
    list_display = ["observation", "created_at", "size", "sha256"]


@admin.register(SiteVisitAccess)
class SiteVisitAccessAdmin(_LookOnly):
    list_display = ["violation", "user_name", "role", "action", "at", "detail"]
    list_filter = ["action"]
