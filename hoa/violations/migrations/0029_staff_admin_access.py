from django.conf import settings
from django.db import migrations


def grant(apps, schema_editor):
    """Managers and board members now have the same access: give existing board logins the admin screens the managers already had."""
    if not getattr(settings, "STAFF_ADMIN_ACCESS", True):
        return
    Profile = apps.get_model("violations", "Profile")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    ids = Profile.objects.filter(role__in=["manager", "board"]).values_list("user_id", flat=True)
    User.objects.filter(pk__in=list(ids)).update(is_staff=True, is_superuser=True)


class Migration(migrations.Migration):
    dependencies = [("violations", "0028_mailpiece")]
    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
