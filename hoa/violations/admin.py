from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from . import accounts
from .models import CannedReply, CommitteeMeeting, CommitteeMember, Owner, Profile, Violation, ViolationType


@admin.action(description="Send portal invitation (creates homeowner login)")
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
        n += 1
    messages.success(request, f"{n} owner(s) marked inactive. Find them under Former owners.")


@admin.register(Owner)
class OwnerAdmin(admin.ModelAdmin):
    search_fields = ["name", "property_address", "email"]
    list_display = ["name", "property_address", "email", "tenant_name", "active", "inactive_since", "last_synced_at"]
    list_filter = ["active"]
    actions = [invite, mark_inactive]


admin.site.register(ViolationType, list_display=["name", "reference", "default_cure_days", "default_fine"])
admin.site.register(CommitteeMeeting, list_display=["starts_at", "zoom_link"])
admin.site.register(CommitteeMember, list_display=["name", "role", "year", "active"], list_filter=["year"])
admin.site.register(CannedReply, list_display=["title", "created_by", "created_at"])


@admin.register(Violation)
class ViolationAdmin(admin.ModelAdmin):
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
class UserAdminWithRole(UserAdmin):
    inlines = [ProfileInline]
    list_display = ["username", "email", "first_name", "last_name", "is_staff"]
