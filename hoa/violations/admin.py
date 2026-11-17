from django.contrib import admin

from .models import Owner, ViolationType

admin.site.register(Owner, search_fields=["name", "property_address", "email"],
                    list_display=["name", "property_address", "email"])
admin.site.register(ViolationType, list_display=["name", "reference", "default_cure_days", "default_fine"])
