from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from violations.models import Profile
from violations.management import dev_guard


class Command(BaseCommand):
    help = "Create (or reset) a login for a staff role: manager, board or committee. For testing; set a real password for real people."

    def add_arguments(self, p):
        p.add_argument("role", choices=["manager", "board", "committee"])
        p.add_argument("--username", default="")
        p.add_argument("--password", default="demo-password-123")
        p.add_argument("--name", default="", help="Full name shown in the app.")
        p.add_argument("--email", default="")

    def handle(self, *a, **o):
        dev_guard.real_password(o["password"])
        role = o["role"]
        username = o["username"] or f"{role}1"
        U = get_user_model()
        u, _ = U.objects.get_or_create(username=username, defaults=dict(email=o["email"]))
        if o["name"]:
            first, _, last = o["name"].partition(" ")
            u.first_name, u.last_name = first, last
        if o["email"]:
            u.email = o["email"]
        u.set_password(o["password"])
        u.save()
        Profile.objects.update_or_create(user=u, defaults={"role": role, "owner": None})
        self.stdout.write(f"Done. {role.capitalize()} login: '{username}' / '{o['password']}'. "
                          + "Two-step login is required for this role unless MFA_REQUIRED_ROLES=none is set on your test copy.")
