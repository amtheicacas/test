from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from violations.models import Owner, Profile
from violations.management import dev_guard


class Command(BaseCommand):
    help = ("Create (or update) a TEST owner whose email is yours, plus a homeowner login for it, so you can send a violation "
            "to that owner and follow the whole process from both sides. Never run in production.")

    def add_arguments(self, p):
        p.add_argument("--email", required=True, help="Your own email: the test notices are emailed here.")
        p.add_argument("--name", default="Test Homeowner")
        p.add_argument("--address", default="1 Test Lane", help="Property address (must be unique).")
        p.add_argument("--username", default="testowner")
        p.add_argument("--password", default="demo-password-123")

    def handle(self, *a, **o):
        dev_guard.test_site_only("make_test_owner")
        if "@" not in o["email"]:
            raise CommandError("Give a real email address with --email.")
        owner, _ = Owner.objects.update_or_create(
            property_address=o["address"],
            defaults=dict(name=o["name"], email=o["email"], active=True, city="Sarasota", state="FL"))
        U = get_user_model()
        u, new = U.objects.get_or_create(username=o["username"], defaults=dict(email=o["email"]))
        u.email = o["email"]
        u.set_password(o["password"])
        u.save()
        Profile.objects.update_or_create(user=u, defaults=dict(role="homeowner", owner=owner))
        self.stdout.write(
            f"Test owner ready: {owner.name}, {owner.property_address}, notices go to {owner.email}.\n"
            f"Owner login: username '{o['username']}', password '{o['password']}'.")
