from django.db import migrations
from django.utils import timezone


def mark_read(apps, schema_editor):
    apps.get_model("violations", "Response").objects.filter(read_at__isnull=True).update(read_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("violations", "0025_response_read_at")]
    operations = [migrations.RunPython(mark_read, migrations.RunPython.noop)]
