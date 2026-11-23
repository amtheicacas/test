"""Phase 4: controlled sign-in email changes (SEC-05), account audit log (SEC-10), admin limits (SEC-09), two-step reset (SEC-14),
session hygiene (SEC-17)."""
import importlib
from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, RequestFactory
from django.urls import clear_url_caches, reverse
from django.utils import timezone

from violations import accounts, audit, services, vantaca
from violations.models import AuditLog, CannedReply, CommitteeMeeting, CommitteeMember, LoginLink, Owner, Profile

pytestmark = pytest.mark.django_db
U = get_user_model()


def make_user(name, role, owner=None, rel="owner", email=None, first="Pat", last="Person"):
    u = U.objects.create_user(name, email or f"{name}@example.com", "x", first_name=first, last_name=last)
    Profile.objects.create(user=u, role=role, owner=owner, relationship=rel)
    return u


@pytest.fixture
def w():
    oa = Owner.objects.create(name="Alex Owner", property_address="1 Case Way", email="alex@example.com", co_owner_email="co@example.com",
                              tenant_name="Tina", tenant_email="tenant@example.com")
    ob = Owner.objects.create(name="Bea Other", property_address="2 Other Way", email="bea@example.com")
    out = dict(oa=oa, ob=ob)
    out["board"], out["manager"] = make_user("board1", "board", first="Bo", last="Ard"), make_user("mgr1", "manager", first="Ma", last="Nager")
    out["committee"] = make_user("comm1", "committee")
    out["home"] = make_user("alex@example.com", "homeowner", oa, email="alex@example.com", first="Alex", last="Owner")
    out["co"] = make_user("co@example.com", "homeowner", oa, "co_owner", email="co@example.com")
    out["tenant"] = make_user("tenant@example.com", "homeowner", oa, "tenant", email="tenant@example.com")
    out["other"] = make_user("bea@example.com", "homeowner", ob, email="bea@example.com")
    return type("W", (), out)


def prof(u):
    return u.profile


def sign_in_session(user):
    c = Client()
    c.force_login(user)
    return c


# ============================== SEC-05: change a sign-in email ==============================
def test_changing_a_sign_in_email_does_everything_in_one_step(w):
    owner_client = sign_in_session(w.home)                              # an open session on the old address
    accounts.send_signin_link(w.home)                                   # and an unused link
    token_link = LoginLink.objects.get(user=w.home)
    mail.outbox.clear()
    r = accounts.change_signin_email(prof(w.home), "New.Address@Example.com", w.board, "Owner phoned; confirmed by phone")
    w.home.refresh_from_db(); w.oa.refresh_from_db()
    assert w.home.username == w.home.email == "new.address@example.com" and w.oa.email == "new.address@example.com"
    token_link.refresh_from_db(); assert token_link.used_at is not None                                 # the old link is dead
    assert owner_client.get("/me/").status_code in (302, 403)                                           # the old session is dead
    assert not any(s.get_decoded().get("_auth_user_id") == str(w.home.pk) for s in Session.objects.all())
    to = {m.to[0]: m for m in mail.outbox}
    assert "alex@example.com" in to and "new.address@example.com" in to
    assert "was changed" in to["alex@example.com"].subject and "n***@example.com" in to["alex@example.com"].body
    assert "new.address@example.com" not in to["alex@example.com"].body                                  # the notice never shows the full new address
    assert "sign-in link" in to["new.address@example.com"].subject.lower()
    assert r["notice_sent"] and r["link_sent"]
    log = AuditLog.objects.get(action="signin_email.changed")
    assert log.actor == w.board and log.actor_name == "Bo Ard" and "alex@example.com -> new.address@example.com" in log.detail
    assert "Owner phoned" in log.detail and "1 Case Way" in log.subject


def test_the_audit_entry_never_holds_the_sign_in_token(w):
    accounts.change_signin_email(prof(w.home), "n@example.com", w.board, "reason")
    sent = [m for m in mail.outbox if "n@example.com" in m.to][0]
    token = sent.body.split("/accounts/link/")[1].split("/")[0].split()[0]
    assert token and all(token not in e.detail and token not in e.subject for e in AuditLog.objects.all())


def test_only_staff_may_change_it_and_a_reason_is_required(w):
    for who in (w.home, w.committee, w.other):
        with pytest.raises(accounts.AccountError, match="Only the Board"):
            accounts.change_signin_email(prof(w.home), "n@example.com", who, "x")
    with pytest.raises(accounts.AccountError, match="Say why"):
        accounts.change_signin_email(prof(w.home), "n@example.com", w.board, "   ")
    w.home.refresh_from_db(); assert w.home.email == "alex@example.com" and not AuditLog.objects.exists()


@pytest.mark.parametrize("bad", ["", "not-an-email", "a@b", "x" * 200 + "@example.com"])
def test_a_bad_address_is_refused(w, bad):
    with pytest.raises(accounts.AccountError):
        accounts.change_signin_email(prof(w.home), bad, w.board, "r")


def test_an_address_already_used_is_refused_so_nobody_gets_two_logins_or_a_strangers_records(w):
    with pytest.raises(accounts.AccountError, match="already uses"):
        accounts.change_signin_email(prof(w.home), "BEA@example.com", w.board, "r")                 # another login's address, any capitals
    Owner.objects.create(name="Cy", property_address="3 Third Way", email="listed-only@example.com")      # on another household's record, no login yet
    with pytest.raises(accounts.AccountError, match="different household"):
        accounts.change_signin_email(prof(w.home), "listed-only@example.com", w.board, "r")
    with pytest.raises(accounts.AccountError, match="already this"):
        accounts.change_signin_email(prof(w.home), "ALEX@example.com", w.board, "r")
    w.home.refresh_from_db(); assert w.home.email == "alex@example.com"


def test_each_kind_of_household_member_updates_its_own_field(w):
    accounts.change_signin_email(prof(w.co), "co2@example.com", w.board, "r")
    accounts.change_signin_email(prof(w.tenant), "tenant2@example.com", w.board, "r")
    w.oa.refresh_from_db()
    assert w.oa.co_owner_email == "co2@example.com" and w.oa.tenant_email == "tenant2@example.com" and w.oa.email == "alex@example.com"
    other = make_user("hh@example.com", "homeowner", w.oa, "other")
    accounts.change_signin_email(prof(other), "hh2@example.com", w.board, "r")
    w.oa.refresh_from_db(); assert w.oa.email == "alex@example.com" and w.oa.co_owner_email == "co2@example.com"       # "other" has no owner field


def test_a_mail_failure_does_not_undo_the_change_and_is_recorded(w):
    with mock.patch("django.core.mail.message.EmailMessage.send", side_effect=OSError("smtp down")):
        r = accounts.change_signin_email(prof(w.home), "n@example.com", w.board, "r")
    w.home.refresh_from_db()
    assert w.home.email == "n@example.com" and not r["notice_sent"] and not r["link_sent"]
    assert AuditLog.objects.filter(action="signin_email.mail_failed").count() == 2


def test_the_login_is_a_login_for_that_household_only_and_the_sessions_of_others_survive(w):
    other_client = sign_in_session(w.other)
    accounts.change_signin_email(prof(w.home), "n@example.com", w.board, "r")
    assert other_client.get("/me/").status_code == 200


def test_the_vantaca_sync_changing_a_login_email_now_cancels_links_ends_sessions_audits_and_notifies(w, django_capture_on_commit_callbacks):
    c = sign_in_session(w.home)
    accounts.send_signin_link(w.home)
    mail.outbox.clear()
    w.oa.email = "vantaca-new@example.com"; w.oa.save()
    with django_capture_on_commit_callbacks(execute=True):          # the old address is told once the change is really saved (review pass 4, R4-10)
        assert accounts.sync_account(w.oa) == "updated"
    w.home.refresh_from_db()
    assert w.home.email == "vantaca-new@example.com"
    assert not LoginLink.objects.filter(user=w.home, used_at__isnull=True).exists()
    assert not any(s.get_decoded().get("_auth_user_id") == str(w.home.pk) for s in Session.objects.all())
    e = AuditLog.objects.get(action="signin_email.changed")
    assert "Vantaca sync" in e.detail and e.actor is None and e.actor_name == "system"
    assert [m.to[0] for m in mail.outbox] == ["alex@example.com"]


def test_a_sync_that_changes_who_owns_a_home_or_where_emails_go_is_audited(w):
    rows = [{"Homeowner ID": "H9", "Account Number": "A9", "Homeowner(s)": "Alex Owner", "Address": "1 Case Way", "Email": "alex@example.com"}]
    vantaca.sync_rows(rows)
    rows[0]["Email"] = "alex-new@example.com"
    rows[0]["Homeowner(s)"] = "Alex Q Owner"
    vantaca.sync_rows(rows, actor=w.manager)
    e = AuditLog.objects.get(action="owner.changed")
    assert "email: alex@example.com -> alex-new@example.com" in e.detail and "name: Alex Owner -> Alex Q Owner" in e.detail
    s = AuditLog.objects.filter(action="owners.synced").latest("pk")
    assert s.actor == w.manager and "updated: 1" in s.detail


# --- the staff pages ---
def test_staff_change_a_sign_in_email_from_the_household_page(client, w):
    client.force_login(w.manager)
    url = reverse("owner_logins", args=[w.oa.pk])
    assert b"Sign-in logins" in client.get(url).content
    r = client.post(url, {"profile": prof(w.home).pk, "action": "change_email", "new_email": "n@example.com", "reason": "phoned"}, follow=True)
    assert "Sign-in email changed" in r.content.decode() and "Vantaca" in r.content.decode()
    w.home.refresh_from_db(); assert w.home.email == "n@example.com"
    r = client.post(url, {"profile": prof(w.home).pk, "action": "change_email", "new_email": "bea@example.com", "reason": "x"}, follow=True)
    assert "already uses" in r.content.decode()
    r = client.post(url, {"profile": prof(w.home).pk, "action": "change_email", "new_email": "z@example.com", "reason": ""}, follow=True)
    assert "Say why" in r.content.decode()


def test_a_login_from_another_household_cannot_be_changed_through_this_household_page(client, w):
    client.force_login(w.manager)
    r = client.post(reverse("owner_logins", args=[w.oa.pk]), {"profile": prof(w.other).pk, "action": "change_email", "new_email": "x@example.com", "reason": "r"})
    assert r.status_code == 404
    w.other.refresh_from_db(); assert w.other.email == "bea@example.com"


@pytest.mark.parametrize("path", ["/owners/{o}/logins/", "/staff-logins/", "/audit/"])
def test_the_new_staff_pages_are_for_board_and_manager_only(client, w, path):
    url = path.format(o=w.oa.pk)
    assert client.get(url).status_code == 302                                  # signed out: to the sign-in page
    for who in (w.home, w.other, w.committee):
        client.force_login(who)
        assert client.get(url).status_code == 403
        assert client.post(url, {"action": "reset_two_step", "user": w.board.pk, "reason": "x"}).status_code == 403
    for who in (w.board, w.manager):
        client.force_login(who)
        assert client.get(url).status_code == 200


def test_staff_sign_a_person_out_everywhere(client, w):
    c = sign_in_session(w.home)
    client.force_login(w.manager)
    client.post(reverse("owner_logins", args=[w.oa.pk]), {"profile": prof(w.home).pk, "action": "sign_out"})
    assert c.get("/me/").status_code in (302, 403)
    assert AuditLog.objects.get(action="sessions.ended").actor == w.manager


def test_end_sessions_touches_only_that_person(w):
    a, b = sign_in_session(w.home), sign_in_session(w.other)
    assert accounts.end_sessions(w.home) == 1
    assert a.get("/me/").status_code in (302, 403) and b.get("/me/").status_code == 200


# ============================== SEC-14: two-step reset ==============================
def device(user):
    from django_otp.plugins.otp_totp.models import TOTPDevice
    return TOTPDevice.objects.create(user=user, name="phone", confirmed=True)


def test_a_different_board_member_can_reset_a_lost_phone(w):
    device(w.board)
    sign_in_session(w.board)
    mail.outbox.clear()
    r = accounts.reset_two_step(w.board, w.manager, "Called him at home and confirmed his voice; phone lost")
    from django_otp import user_has_device
    assert not user_has_device(w.board) and r == {"devices": 1, "signed_out": 1, "emailed": True}
    e = AuditLog.objects.get(action="two_step.reset")
    assert e.actor == w.manager and "confirmed his voice" in e.detail and e.subject == "Bo Ard"
    assert mail.outbox[0].to == ["board1@example.com"] and "reset" in mail.outbox[0].subject


def test_nobody_can_reset_their_own_and_a_reason_is_required_and_only_staff_may(w):
    device(w.board)
    with pytest.raises(accounts.AccountError, match="your own"):
        accounts.reset_two_step(w.board, w.board, "x")
    with pytest.raises(accounts.AccountError, match="Say how"):
        accounts.reset_two_step(w.board, w.manager, "")
    for who in (w.committee, w.home):
        with pytest.raises(accounts.AccountError, match="Only the Board"):
            accounts.reset_two_step(w.board, who, "x")
    with pytest.raises(accounts.AccountError, match="nothing to reset"):
        accounts.reset_two_step(w.manager, w.board, "x")
    from django_otp import user_has_device
    assert user_has_device(w.board) and not AuditLog.objects.exists()


def test_the_reset_works_from_the_staff_logins_page(client, w):
    device(w.committee)
    client.force_login(w.board)
    page = client.get("/staff-logins/").content.decode()
    assert "Reset two-step" in page and "comm1" in page
    r = client.post("/staff-logins/", {"user": w.committee.pk, "action": "reset_two_step", "reason": "confirmed in person"}, follow=True)
    assert "Two-step sign-in reset" in r.content.decode()
    r = client.post("/staff-logins/", {"user": w.board.pk, "action": "reset_two_step", "reason": "x"}, follow=True)
    assert "your own" in r.content.decode()


def test_the_totp_secret_is_never_shown_in_the_admin_and_devices_cannot_be_deleted_there(client, w):
    from django.contrib import admin
    from django_otp.plugins.otp_totp.models import TOTPDevice
    d = device(w.committee)
    ma = admin.site._registry[TOTPDevice]
    w.manager.refresh_from_db()
    assert "key" in ma.exclude and not ma.has_delete_permission(_req(w.manager), d) and not ma.has_change_permission(_req(w.manager), d) \
        and not ma.has_add_permission(_req(w.manager))
    client.force_login(w.manager)
    page = client.get(reverse("admin:otp_totp_totpdevice_change", args=[d.pk]))
    assert page.status_code == 200 and d.key not in page.content.decode()


# ============================== SEC-17: nothing private is cached ==============================
def test_signed_in_pages_are_never_stored_by_the_browser(client, w):
    client.force_login(w.home)
    assert "no-store" in client.get("/me/")["Cache-Control"]
    client.force_login(w.manager)
    assert "no-store" in client.get("/workspace/")["Cache-Control"]
    assert "no-store" in client.get("/audit/")["Cache-Control"]


def test_a_view_that_sets_its_own_cache_header_keeps_it(client, w):
    from django.core.files.base import ContentFile
    from violations.models import Violation, ViolationType, Notice, SentLetter
    v = Violation.objects.create(owner=w.oa, vtype=ViolationType.objects.create(name="T"), description="d", cure_required="c")
    n = Notice.objects.create(violation=v, kind="violation")
    import hashlib
    data = b"%PDF-1.4 x"
    s = SentLetter(violation=v, notice=n, kind="violation", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    s.file.save("l.pdf", ContentFile(data), save=False); s.save()
    client.force_login(w.manager)
    assert client.get(reverse("sent_letter", args=[s.pk]))["Cache-Control"] == "private, max-age=300"


def test_static_files_and_responses_that_set_their_own_header_are_left_alone(w):
    from django.http import HttpResponse
    from violations.middleware import NoStorePrivatePages
    rf = RequestFactory()
    req = rf.get("/static/app.css"); req.user = w.home
    assert "Cache-Control" not in NoStorePrivatePages(lambda r: HttpResponse("x"))(req)
    req = rf.get("/me/"); req.user = w.home
    assert NoStorePrivatePages(lambda r: HttpResponse("x"))(req)["Cache-Control"] == "no-store, private"
    anon = rf.get("/me/"); anon.user = type("A", (), {"is_authenticated": False})()
    assert "Cache-Control" not in NoStorePrivatePages(lambda r: HttpResponse("x"))(anon)


# ============================== SEC-09: admin limits ==============================
def _req(user):
    rf = RequestFactory().get("/")
    rf.user = user
    return rf


@pytest.mark.parametrize("model", ["Owner", "Violation", "CommitteeMeeting", "CommitteeMember", "CannedReply", "ViolationType", "User"])
def test_records_cannot_be_deleted_in_the_admin_on_a_live_site(w, settings, model):
    from django.contrib import admin
    from violations import models as m
    cls = U if model == "User" else getattr(m, model)
    ma = admin.site._registry[cls]
    w.manager.refresh_from_db()                                                   # (Profile.save made the login a superuser in the database)
    settings.DEBUG = False
    assert not ma.has_delete_permission(_req(w.manager))
    settings.DEBUG = True
    assert ma.has_delete_permission(_req(w.manager))                           # a development site can still clear test data


def test_owner_email_fields_are_read_only_in_the_admin_once_the_owner_exists(w):
    from django.contrib import admin
    ma = admin.site._registry[Owner]
    ro = ma.get_readonly_fields(_req(w.manager), w.oa)
    assert {"email", "additional_emails", "co_owner_email", "tenant_email", "tenant_additional_emails"} <= set(ro)
    assert "email" not in ma.get_readonly_fields(_req(w.manager), None)           # filling it in for a brand new owner is fine


def test_admin_posts_cannot_change_an_owners_email(client, w):
    client.force_login(w.manager)
    url = reverse("admin:violations_owner_change", args=[w.oa.pk])
    form = client.get(url).context["adminform"].form
    assert "email" not in form.fields and "co_owner_email" not in form.fields


def test_admin_changes_are_written_to_the_audit_log_without_passwords(w):
    from django.contrib import admin
    ma = admin.site._registry[U]
    form = type("F", (), {"changed_data": ["first_name", "password", "last_login"], "initial": {"first_name": "Old"}, "cleaned_data": {"first_name": "New", "password": "SECRET-HASH"}})()
    ma.save_model(_req(w.manager), w.home, form, True)
    e = AuditLog.objects.get(action="admin.user.changed")
    assert "first_name: Old -> New" in e.detail and "password changed" in e.detail and "SECRET-HASH" not in e.detail
    assert e.actor == w.manager and e.subject == str(w.home)


def test_a_role_change_made_in_the_admin_is_audited(w):
    from django.contrib import admin
    ma = admin.site._registry[U]
    p = prof(w.committee)
    f = type("Fm", (), {})()
    f.instance = p
    formset = type("FS", (), {"forms": [f], "save": lambda self: None})()
    base = mock.patch.object(type(ma).__mro__[2], "save_formset", lambda *a, **k: setattr(p, "role", "board"))    # UserAdmin.save_formset
    with base:
        ma.save_formset(_req(w.manager), type("F", (), {"instance": w.committee})(), formset, True)
    e = AuditLog.objects.get(action="admin.profile.changed")
    assert "role: committee -> board" in e.detail


def test_the_admin_address_can_be_moved(settings):
    import config.urls
    try:
        settings.ADMIN_URL = "board-tools-7k2/"
        importlib.reload(config.urls)
        clear_url_caches()
        c = Client()
        assert c.get("/board-tools-7k2/login/").status_code == 200
        assert c.get("/admin/login/").status_code == 404
    finally:
        settings.ADMIN_URL = "admin/"
        importlib.reload(config.urls)
        clear_url_caches()


def test_the_admin_address_defaults_to_admin_and_has_one_trailing_slash():
    from django.conf import settings as s
    assert s.ADMIN_URL == "admin/" and reverse("admin:index") == "/admin/"


# ============================== SEC-10: the audit log ==============================
def test_audit_rows_can_never_be_changed_or_deleted(w):
    e = audit.record("x.test", "subject", "detail", actor=w.board)
    e.detail = "changed"
    with pytest.raises(ValidationError):
        e.save()
    with pytest.raises(PermissionDenied):
        e.delete()
    from django.contrib import admin
    ma = admin.site._registry[AuditLog]
    assert not ma.has_add_permission(None) and not ma.has_change_permission(None) and not ma.has_delete_permission(None)


def test_the_actors_name_survives_the_login_being_removed(w):
    audit.record("x.test", "s", actor=w.committee)
    w.committee.delete()
    e = AuditLog.objects.get(action="x.test")
    assert e.actor is None and e.actor_name == "Pat Person"


def test_audit_records_the_visitors_address(client, w):
    client.force_login(w.manager)
    client.post(reverse("owner_logins", args=[w.oa.pk]), {"profile": prof(w.home).pk, "action": "sign_out"}, REMOTE_ADDR="203.0.113.9")
    assert AuditLog.objects.get(action="sessions.ended").ip_address == "203.0.113.9"


def test_the_audit_page_lists_and_searches(client, w):
    audit.record("owner.changed", "1 Case Way", "email: a -> b", actor=w.manager)
    audit.record("roster.added", "Someone", "", actor=w.board)
    client.force_login(w.board)
    page = client.get("/audit/").content.decode()
    assert "owner.changed" in page and "roster.added" in page and "Ma Nager" in page
    page = client.get("/audit/?q=Case+Way").content.decode()
    assert "owner.changed" in page and "roster.added" not in page


def test_meetings_roster_replies_and_minutes_leave_a_trail(client, w):
    client.force_login(w.manager)
    m = services.create_meeting(timezone.now() + timedelta(days=30), zoom_link="https://zoom.example/j/1", user=w.manager)
    services.reschedule_meeting(m, timezone.now() + timedelta(days=40), w.manager, notify=False)
    client.post("/committee/members/", {"action": "add", "name": "Pat Newmember", "year": timezone.localdate().year, "role": "member"})
    kinds = set(AuditLog.objects.values_list("action", flat=True))
    assert {"meeting.created", "meeting.rescheduled", "roster.added"} <= kinds
    services.cancel_meeting(m, w.manager, notify=False)
    assert AuditLog.objects.filter(action="meeting.cancelled").exists()
    assert not CommitteeMeeting.objects.filter(pk=m.pk).exists()                  # the meeting is gone but its trail remains


def test_standard_responses_and_reactivation_and_minutes_are_audited(client, w):
    client.force_login(w.manager)
    url = reverse("canned_replies")
    client.post(url, {"action": "add", "title": "Thanks", "body": "Thank you."})
    cr = CannedReply.objects.get(title="Thanks")
    client.post(url, {"action": "update", "id": cr.pk, "title": "Thanks 2", "body": "Thank you."})
    client.post(url, {"action": "delete", "id": cr.pk})
    assert [e.action for e in AuditLog.objects.filter(action__startswith="standard_response").order_by("pk")] == \
        ["standard_response.added", "standard_response.changed", "standard_response.deleted"]
    w.ob.active = False; w.ob.save()
    client.post(reverse("owner_archive"), {"id": w.ob.pk})
    assert AuditLog.objects.filter(action="owner.reactivated", subject="2 Other Way").exists()
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=3))
    from violations.tests.test_minutes_files import pdf_bytes, up
    client.post(f"/committee/meetings/{m.pk}/save/", {"attendees": "x", "minutes": up("m.pdf", pdf_bytes())})
    e = AuditLog.objects.get(action="minutes.uploaded")
    assert "m.pdf" in e.detail and e.actor == w.manager
    client.post(f"/committee/meetings/{m.pk}/save/", {"attendees": "x", "minutes": up("m2.pdf", pdf_bytes("two"))})
    assert "replacing" in AuditLog.objects.filter(action="minutes.uploaded").latest("pk").detail


def test_the_staff_navigation_links_to_the_new_pages(client, w):
    client.force_login(w.manager)
    page = client.get("/workspace/").content.decode()
    assert "/staff-logins/" in page and "/audit/" in page
    client.force_login(w.home)
    assert "/staff-logins/" not in client.get("/me/").content.decode()
