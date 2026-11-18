from django.db import migrations

DEFAULTS = [
    ("Reply received - will verify", "Thank you. Your reply has been received. We will verify the correction at our next inspection."),
    ("Photo of the correction requested", "Thank you for your response. To help us close this out, please upload a photo of the corrected area using the message box on this page."),
    ("Extension request received", "We received your request for more time. The Board will review it and we will let you know its decision. Please keep working toward the current deadline in the meantime."),
    ("Verified corrected - closing", "Thank you for correcting the violation. We have reinspected and verified the correction, so this matter is now closed."),
]


def add(apps, schema_editor):
    C = apps.get_model("violations", "CannedReply")
    for t, b in DEFAULTS:
        C.objects.get_or_create(title=t, defaults={"body": b})


class Migration(migrations.Migration):
    dependencies = [("violations", "0020_cannedreply")]
    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
