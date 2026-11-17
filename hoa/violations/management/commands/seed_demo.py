import io
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone
from PIL import Image, ImageDraw

from violations import services
from violations.models import CommitteeMeeting, Owner, Photo, Profile, Violation, ViolationType

S = Violation.Status


def img(label):
    im = Image.new("RGB", (640, 400), (120, 160, 110))
    ImageDraw.Draw(im).text((20, 20), label, fill="white")
    b = io.BytesIO()
    im.save(b, "JPEG")
    return ContentFile(b.getvalue(), "demo.jpg")


class Command(BaseCommand):
    help = "Create demo owners, cases and one login per role (password: demo-password-123). Never run in production."

    def handle(self, *a, **kw):
        U = get_user_model()
        homes = [("Pat Owner", "12 Heron Way", 27.3400, -82.5300), ("Sam Lee", "14 Heron Way", 27.3402, -82.5294),
                 ("Ana Ruiz", "16 Heron Way", 27.3404, -82.5288), ("Kim Park", "18 Heron Way", 27.3406, -82.5282),
                 ("Lou Gray", "20 Heron Way", 27.3408, -82.5276), ("Joe Bell", "22 Heron Way", 27.3410, -82.5270)]
        owners = [Owner.objects.get_or_create(property_address=ad, defaults=dict(
            name=n, email=f"{n.split()[0].lower()}@example.com", zip_code="34240", lat=la, lng=lo))[0]
            for n, ad, la, lo in homes]
        shed = ViolationType.objects.get_or_create(name="Unapproved shed", defaults=dict(reference="Declaration Art. VII §3"))[0]
        trash = ViolationType.objects.get_or_create(name="Trash cans visible", defaults=dict(reference="Rules §4.2"))[0]
        spec = [(owners[0], shed, S.NOTICE_SENT), (owners[1], trash, S.PENDING_BOARD), (owners[2], shed, S.HEARING_NEEDED),
                (owners[3], trash, S.HEARING_SCHEDULED), (owners[4], shed, S.CURED), (owners[5], trash, S.ON_HOLD)]
        meeting = CommitteeMeeting.objects.get_or_create(
            starts_at=timezone.now() + timedelta(days=21), defaults=dict(zoom_link="https://zoom.us/j/1234567890"))[0]
        for o, t, st in spec:
            if Violation.objects.filter(owner=o).exists():
                continue
            v = Violation.objects.create(owner=o, vtype=t, observed_on=date.today() - timedelta(days=30),
                                         description="Observed from the street; see photo.", cure_required="Correct within the cure period.",
                                         cure_days=15, cure_deadline=date.today() + (timedelta(days=9) if st == S.NOTICE_SENT else -timedelta(days=15)))
            Photo.objects.create(violation=v, image=img(f"{t.name} - {o.property_address}"))
            services.send_notice(v, "violation")
            v.status = st
            if st in (S.HEARING_NEEDED, S.HEARING_SCHEDULED):
                v.proposed_fine, v.board_meeting_date = 100, date.today() - timedelta(days=5)
            if st == S.HEARING_SCHEDULED:
                v.committee_meeting, v.hearing_date, v.hearing_location = meeting, meeting.starts_at, meeting.place
                services.send_notice(v, "hearing")
            if st == S.ON_HOLD:
                v.held_from, v.hold_reason, v.held_on = S.NOTICE_SENT, "Owner hospitalized", date.today()
            v.save()
        for role, owner in [("manager", None), ("board", None), ("committee", None), ("homeowner", owners[0])]:
            u, new = U.objects.get_or_create(username=role, defaults=dict(email=f"{role}@example.com"))
            if new:
                u.set_password("demo-password-123")
                u.save()
            Profile.objects.get_or_create(user=u, defaults=dict(role=role, owner=owner))
        self.stdout.write("Demo ready. Log in as manager / board / committee / homeowner, password demo-password-123")
