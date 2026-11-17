from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from . import accounts
from .models import CommitteeMeeting, Owner, Profile, ViolationType


@admin.action(description="Send portal invitation (creates homeowner login)")
def invite(modeladmin, request, queryset):
    sent = sum(1 for o in queryset if accounts.invite_owner(o, request))
    messages.success(request, f"{sent} invitation(s) sent. Owners without an email or already invited were skipped.")


@admin.register(Owner)
class OwnerAdmin(admin.ModelAdmin):
    search_fields = ["name", "property_address", "email"]
    list_display = ["name", "property_address", "email", "tenant_name", "last_synced_at"]
    actions = [invite]


admin.site.register(ViolationType, list_display=["name", "reference", "default_cure_days", "default_fine"])
admin.site.register(CommitteeMeeting, list_display=["starts_at", "zoom_link"])


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False


User = get_user_model()
admin.site.unregister(User)


@admin.register(User)
class UserAdminWithRole(UserAdmin):
    inlines = [ProfileInline]
    list_display = ["username", "email", "first_name", "last_name", "is_staff"]
