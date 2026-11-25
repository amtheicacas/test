"""Review pass 4 (R4-12): every text column filled to its LIMIT, then the whole workflow (notice, more time, Board vote, meeting, committee decision, the Board's review,
every kind of letter, the case file, the attorney draft) and every staff page. PostgreSQL (the live database) refuses text longer than a column; SQLite accepts it, so a
crash of that kind is only seen on PostgreSQL: run the suite with DATABASE_URL set to see this test do its real job. (Words are separated by spaces so the letters can wrap.)"""
from datetime import date, timedelta

import pytest
from django.core.files.base import ContentFile
from django.db import models as m
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from violations import casefile, services
from violations.models import CommitteeMeeting, CommitteeMember, Owner, Photo, Profile, Violation, ViolationType
from violations.pdf import render_notice_pdf

pytestmark = pytest.mark.django_db


def full(model, **over):
    """kwargs for `model` with every CharField at its max_length (valid-looking text)."""
    kw = {}
    for f in model._meta.concrete_fields:
        if isinstance(f, m.EmailField):
            local = "x" * (f.max_length - len("@example.com"))
            kw[f.name] = (local + "@example.com")[:f.max_length]
        elif isinstance(f, m.URLField):
            kw[f.name] = ("https://example.com/" + "a" * f.max_length)[:f.max_length]
        elif isinstance(f, m.CharField) and f.max_length and not f.choices and not f.primary_key:
            kw[f.name] = ("Word " * f.max_length)[:f.max_length].strip()
    kw.update(over)
    return kw


def test_R4_12_everything_at_its_limit_goes_through_the_whole_workflow_and_every_page(settings, django_user_model):
    settings.MAIL_PROVIDER = "manual"
    trouble = []

    def step(name, fn):
        try:
            return fn()
        except Exception as e:      # noqa: BLE001
            trouble.append(f"{name}: {type(e).__name__}: {str(e)[:150]}")

    o = step("owner", lambda: Owner.objects.create(**full(Owner, state="FL", mail_state="FL", external_id="X" * 100, active=True, lat=None, lng=None,
                                                         additional_emails=";".join(f"a{i}@example.com" for i in range(50)), mailing_address=("Mailing " * 500)[:3000],
                                                         tenant_start=None, tenant_end=None)))
    t = step("type", lambda: ViolationType.objects.create(**full(ViolationType, code=None, section="1.1", default_cure_days=15)))
    if not (o and t):
        pass
    else:
        v = step("violation", lambda: Violation.objects.create(owner=o, vtype=t, description=("Described " * 3000)[:20000], cure_required=("Cured " * 3000)[:20000], cure_days=15,
                                                              status="draft", observed_on=date.today()))
        Photo.objects.create(violation=v, image=ContentFile(b"\xff\xd8\xff\xe0" + b"0" * 100, name="p.jpg"))
        staff = django_user_model.objects.create_user("b4", password="x", first_name="F" * 150, last_name="L" * 150)
        Profile.objects.create(user=staff, role="board")
        step("issue", lambda: services.issue_first_notice(v, staff, timing="urgent"))
        step("extension", lambda: services.grant_extension(v, None, staff, ("reason " * 1000)[:5000], days=7))
        v.refresh_from_db()
        Violation.objects.filter(pk=v.pk).update(status="pending_board")
        v.refresh_from_db()
        step("board", lambda: services.board_decide(v, "proceed", date.today(), staff, ("note " * 1000)[:5000], fine=100, by_majority=True))
        meet = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), **full(CommitteeMeeting, series="", zoom_api_id=""))
        step("assign", lambda: services.assign_to_meeting(v, meet, staff))
        Violation.objects.filter(pk=v.pk).update(hearing_date=timezone.now() - timedelta(days=1))
        v.refresh_from_db()
        CommitteeMember.objects.create(name="N" * 120, year=timezone.localdate().year, user=None)
        step("record", lambda: services.committee_record(v, "confirm", staff, None, 3, 0, ("Finding " * 3000)[:20000], True))
        step("submit", lambda: services.submit_meeting_results(meet, staff))
        v.refresh_from_db()
        step("approve", lambda: services.board_review_item(v, staff, "approve", method="manual", reference="R" * 500, note="x" * 3000))
        v.refresh_from_db()
        for n in v.notices.all():
            for who in ("owner", "owner_property", "tenant"):
                step(f"pdf {n.kind} {who}", lambda: render_notice_pdf(n, who))
        step("casefile", lambda: casefile.build_case_file(v, staff))
        step("legal draft", lambda: services.legal_email_draft(v, staff))
        c = Client(raise_request_exception=False)
        c.force_login(staff)
        for url in (reverse("dashboard"), reverse("board_queue"), reverse("violation_list"), reverse("violation_detail", args=[v.pk]), reverse("meetings"),
                    reverse("meeting_manage", args=[meet.pk]), reverse("committee_meeting", args=[meet.pk]), reverse("results_list"),
                    reverse("results_detail", args=[meet.pk]), reverse("owner_correspondence", args=[o.pk]), reverse("owner_logins", args=[o.pk]),
                    reverse("committee_members"), reverse("map_api"), reverse("audit_log"), reverse("violation_create"), reverse("owner_archive")):
            r = c.get(url)
            if r.status_code >= 500:
                trouble.append(f"GET {url} -> {r.status_code}")
    assert not trouble, trouble
