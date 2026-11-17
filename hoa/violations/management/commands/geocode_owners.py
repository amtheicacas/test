from django.core.management.base import BaseCommand

from violations import geocoding


class Command(BaseCommand):
    help = "Place owners' homes on the map (US Census batch geocoder, free, no key). Only owners without coordinates."

    def handle(self, *a, **kw):
        located, missing = geocoding.geocode_missing()
        self.stdout.write(f"located {located}, not found {missing}")
