from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    """On the live site the counters behind the sign-in limits are kept in a database table (CACHES in settings.py). Creating it here
    means a plain `migrate` is all a deploy needs. On your own PC (in-memory counters) this does nothing."""
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):

    dependencies = [
        ("violations", "0038_event_text_unlimited"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
