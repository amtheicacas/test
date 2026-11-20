from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from violations.models import Owner, Profile
from violations.management import dev_guard


class Command(BaseCommand):
    help = ("Give an EXISTING owner (found by part of the address) a homeowner login with a password you choose, so you can see the "
            "resident portal exactly as that owner does. For testing; real owners get an emailed invitation instead.")

    def add_arguments(self, p):
        p.add_argument("--address", required=True, help='Part of the property address, e.g. "7316 Great Egret".')
        p.add_argument("--username", default="", help="Login name (default: owner-<lot number>).")
        p.add_argument("--password", default="demo-password-123")

    def handle(self, *a, **o):
        dev_guard.real_password(o["password"])
        found = list(Owner.objects.filter(property_address__icontains=o["address"], active=True))
        if not found:
            raise CommandError(f'No active owner matches "{o["address"]}".')
        if len(found) > 1:
            raise CommandError("More than one match, be more specific:\n  " + "\n  ".join(f"{x.property_address} ({x.name})" for x in found))
        owner = found[0]
        username = o["username"] or f"owner-{owner.lot_no or owner.pk}"
        U = get_user_model()
        u, _ = U.objects.get_or_create(username=username, defaults=dict(email=owner.email or ""))
        u.set_password(o["password"])
        u.save()
        Profile.objects.update_or_create(user=u, defaults=dict(role="homeowner", owner=owner))
        self.stdout.write(f"Done. Log in as '{username}' / '{o['password']}' to see the portal for {owner.display_name}, {owner.property_address}.")
