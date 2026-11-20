"""Former owners are kept (3 years), hidden from daily use, searchable, and a sale starts a fresh record."""
from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from violations import accounts, vantaca
from violations.models import CommitteeMember, Owner, Profile, Violation, ViolationType


def row(**kw):
    base = {"Homeowner ID": "H1", "Account Number": "A1", "Homeowner(s)": "Old Owner", "Address": "1 Sale Way", "Email": "old@example.com"}
    base.update(kw)
    return base


def test_a_sale_archives_the_old_owner_and_starts_a_new_record(db):
    vantaca.sync_rows([row()], invite=True)
    old = Owner.objects.get(external_id="H1")
    v = Violation.objects.create(owner=old, vtype=ViolationType.objects.create(name="T"), description="d", cure_required="c")
    vantaca.sync_rows([row(**{"Homeowner ID": "H2", "Account Number": "A2", "Homeowner(s)": "New Owner", "Email": "new@example.com"})])
    old.refresh_from_db()
    new = Owner.objects.get(external_id="H2")
    assert not old.active and old.inactive_since == date.today() and "sold" in old.inactive_reason
    assert new.active and new.pk != old.pk and new.property_address == old.property_address       # same address, two records
    assert old.violations.get() == v and not new.violations.exists()                              # the history stays with the old owner
    assert not Profile.objects.get(owner=old).user.is_active                                      # their login is off
    assert old.keep_until == date.today().replace(year=date.today().year + 3)


def test_missing_from_the_feed_marks_inactive_with_a_reason(db):
    vantaca.sync_rows([row(), row(**{"Homeowner ID": "H3", "Account Number": "A3", "Homeowner(s)": "Stays", "Address": "3 Stay Way"})])
    vantaca.sync_rows([row(**{"Homeowner ID": "H3", "Account Number": "A3", "Homeowner(s)": "Stays", "Address": "3 Stay Way"})], deactivate_missing=True, force=True)
    gone = Owner.objects.get(external_id="H1")
    assert not gone.active and gone.inactive_reason == "No longer in the Vantaca feed" and gone.inactive_since


def test_a_tenant_login_switches_off_when_the_lease_ends(db):
    o = Owner.objects.create(name="Lndlrd", property_address="2 Rent Way", email="l@example.com", tenant_name="Tina", tenant_email="t@example.com",
                             tenant_end=date.today() + timedelta(days=30))
    t = accounts.invite_member(o, "t@example.com", "Tina", "tenant").user
    assert accounts.sync_account(o) is None and t.is_active
    o.tenant_end = date.today() - timedelta(days=1); o.save()
    assert accounts.sync_account(o) == "deactivated"
    t.refresh_from_db(); assert not t.is_active


def test_former_owners_page_searches_reactivates_and_hides_them_elsewhere(db, client, django_user_model):
    old = Owner.objects.create(name="Gone Gary", property_address="4 Away Way", email="gary@example.com", lot_no="44", active=False)
    Owner.objects.create(name="Here Hal", property_address="5 Here Way")
    u = django_user_model.objects.create_user("mg40", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    assert "Gone Gary" in client.get("/owners/archive/").content.decode() and "Here Hal" not in client.get("/owners/archive/").content.decode()
    assert "Gone Gary" in client.get("/owners/archive/?q=44").content.decode() and "Gone Gary" in client.get("/owners/archive/?q=gary@").content.decode()
    assert "Gone Gary" not in client.get("/violations/new/").content.decode()                     # can't be given new violations
    Owner.objects.create(name="Buyer", property_address="4 Away Way")
    client.post("/owners/archive/", {"id": old.pk})
    old.refresh_from_db(); assert not old.active                                                  # address already has an active owner
    Owner.objects.filter(name="Buyer").delete()
    client.post("/owners/archive/", {"id": old.pk})
    old.refresh_from_db(); assert old.active and old.inactive_since is None


def test_purge_only_after_three_years_and_only_with_yes(db):
    keep = Owner.objects.create(name="Recent", property_address="6 Recent Way", active=False)
    old = Owner.objects.create(name="Ancient", property_address="7 Old Way", active=False)
    Owner.objects.filter(pk=old.pk).update(inactive_since=date.today() - timedelta(days=3 * 365 + 5))
    Violation.objects.create(owner=old, vtype=ViolationType.objects.create(name="T"), description="d", cure_required="c")
    call_command("purge_archived_owners")                                                         # dry run
    assert Owner.objects.filter(pk=old.pk).exists()
    call_command("purge_archived_owners", "--yes")
    assert not Owner.objects.filter(pk=old.pk).exists() and Owner.objects.filter(pk=keep.pk).exists() and not Violation.objects.exists()


def test_committee_logins_follow_the_yearly_roster(db, client, django_user_model):
    from django.utils import timezone
    y = timezone.localdate().year
    u = django_user_model.objects.create_user("cmt1", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "committee"})
    CommitteeMember.objects.create(name="Last Year", year=y - 1, user=u)
    assert accounts.sync_committee_logins() == (0, 1)
    u.refresh_from_db(); assert not u.is_active
    CommitteeMember.objects.create(name="Last Year", year=y, user=u)                              # back on this year's roster
    assert accounts.sync_committee_logins() == (1, 0)
    u.refresh_from_db(); assert u.is_active
    m = django_user_model.objects.create_user("mg41", password="x"); Profile.objects.update_or_create(user=m, defaults={"role": "manager"})
    client.force_login(m)
    html = client.get("/committee/members/").content.decode()
    assert "cmt1" in html
