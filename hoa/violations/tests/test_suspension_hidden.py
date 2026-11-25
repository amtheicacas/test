"""A4: suspension is in the Declaration but does not apply today, so the application hides it (SUSPENSION_ENABLED, off by default).
Nothing is deleted. The letters' wording is NOT changed yet: the last test records which suspension phrases they still contain."""
import io
import os
import subprocess
import sys
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from pypdf import PdfReader

from violations import services
from violations.models import CommitteeMeeting, CommitteeMember, Notice, Owner, Profile, Violation, ViolationType
from violations.pdf import render_notice_pdf

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()


def login(client, name, role, owner=None):
    u = U.objects.create_user(name, f"{name}@example.com", "x")
    Profile.objects.create(user=u, role=role, owner=owner)
    client.force_login(u)
    return u


@pytest.fixture
def old_data():
    """Cases that carry suspension data (as an older test site might), in every stage where the screens used to show it."""
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), results_status="submitted")
    CommitteeMember.objects.create(name="Casey Member", year=timezone.localdate().year)
    out = {}
    for i, status in enumerate((S.PENDING_BOARD, S.HEARING_NEEDED, S.HEARING_SCHEDULED, S.LEDGER_PENDING, S.FINE_APPROVED)):
        o = Owner.objects.create(name=f"Owner {i}", property_address=f"{i + 1} Pool Way", email=f"o{i}@example.com")
        v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=status, board_meeting_date=date.today(),
                                     proposed_fine=100, proposed_suspension="the clubhouse pool", proposed_suspension_days=10,
                                     fine_amount=100, suspension="the clubhouse pool", suspension_days=10, fine_due_on=date.today() + timedelta(days=30),
                                     hearing_date=m.starts_at if status in (S.HEARING_SCHEDULED, S.LEDGER_PENDING) else None,
                                     committee_meeting=m if status in (S.HEARING_SCHEDULED, S.LEDGER_PENDING) else None,
                                     committee_outcome="confirm" if status in (S.LEDGER_PENDING, S.FINE_APPROVED) else "",
                                     committee_votes_for=3, committee_votes_against=0)
        out[status] = v
    return out, m


def pages(client, old_data):
    """Every screen that used to show suspension, fetched as the person who sees it. {label: html}"""
    cases, m = old_data
    got = {}
    login(client, "boss", "board")
    for status, v in cases.items():
        got[f"case page ({status})"] = client.get(f"/violations/{v.pk}/").content.decode()
    got["board queue"] = client.get("/board/").content.decode()
    got["dashboard"] = client.get("/workspace/").content.decode()
    got["results list"] = client.get("/results/").content.decode()
    got["results detail"] = client.get(f"/results/{m.pk}/").content.decode()
    login(client, "comm", "committee")
    got["committee home"] = client.get("/committee/").content.decode()
    got["committee meeting"] = client.get(f"/committee/meetings/{m.pk}/").content.decode()
    got["committee case"] = client.get(f"/committee/cases/{cases[S.HEARING_SCHEDULED].pk}/").content.decode()
    for status in (S.HEARING_SCHEDULED, S.FINE_APPROVED):
        v = cases[status]
        login(client, f"own{status}", "homeowner", v.owner)
        got[f"owner page ({status})"] = client.get(f"/me/{v.pk}/").content.decode()
    got["resident link"] = client.get(f"/r/{cases[S.HEARING_SCHEDULED].token}/").content.decode()
    return got


def test_it_is_off_unless_asked_for():
    from violations.tests.test_deploy_safety import HOA_DIR
    code = "import config.settings as s; print(s.SUSPENSION_ENABLED)"
    env = {k: v for k, v in os.environ.items() if k != "SUSPENSION_ENABLED"}
    run = lambda **extra: subprocess.run([sys.executable, "-c", code], cwd=HOA_DIR, env={**env, "DJANGO_DEBUG": "1", "HOA_ENV_FILE": "none", **extra},
                                         capture_output=True, text=True).stdout.strip()
    assert run() == "False" and run(SUSPENSION_ENABLED="1") == "True" and run(SUSPENSION_ENABLED="0") == "False"


def test_no_screen_mentions_suspension_when_it_is_off(client, old_data, settings):
    settings.SUSPENSION_ENABLED = False
    shown = {label for label, html in pages(client, old_data).items() if "suspen" in html.lower() or "clubhouse pool" in html.lower()}
    assert shown == set()           # neither the word nor the data (what was suspended)


def test_the_same_screens_do_show_it_when_it_is_switched_on(client, old_data, settings):
    """(Proves the test above is looking at the right pages, and that switching it back on works.)"""
    settings.SUSPENSION_ENABLED = True
    shown = {label for label, html in pages(client, old_data).items() if "suspen" in html.lower() or "clubhouse pool" in html.lower()}
    assert {"case page (hearing_scheduled)", "case page (fine_approved)", "case page (pending_board)", "committee case", "committee meeting",
            "committee home", "board queue", "dashboard", "owner page (hearing_scheduled)", "owner page (fine_approved)", "results detail"} <= shown


def test_hiding_it_deletes_nothing(old_data, settings):
    settings.SUSPENSION_ENABLED = False
    cases, _ = old_data
    v = Violation.objects.get(pk=cases[S.FINE_APPROVED].pk)
    assert (v.suspension, v.suspension_days, v.proposed_suspension, v.proposed_suspension_days) == ("the clubhouse pool", 10, "the clubhouse pool", 10)


def test_the_board_vote_ignores_a_suspension_when_it_is_off(settings):
    settings.SUSPENSION_ENABLED = False
    o = Owner.objects.create(name="P", property_address="1 Vote St", email="p@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.get_or_create(name="Shed")[0], description="d", cure_required="c", status=S.PENDING_BOARD)
    services.board_decide(v, "proceed", date.today(), fine="100", suspension="the pool", suspension_days=10, by_majority=True)
    v.refresh_from_db()
    assert v.proposed_fine == 100 and v.proposed_suspension == "" and v.proposed_suspension_days is None and v.status == S.HEARING_NEEDED
    v2 = Violation.objects.create(owner=Owner.objects.create(name="Q", property_address="2 Vote St"), vtype=v.vtype, description="d", cure_required="c", status=S.PENDING_BOARD)
    with pytest.raises(services.WorkflowError) as e:
        services.board_decide(v2, "proceed", date.today(), suspension="the pool", suspension_days=10, by_majority=True)     # nothing but a suspension
    assert str(e.value) == "Enter the proposed fine amount the Board voted on."


def test_messages_do_not_mention_suspension_when_it_is_off(settings):
    o = Owner.objects.create(name="P", property_address="3 Msg St", email="p@example.com")
    t = ViolationType.objects.get_or_create(name="Shed")[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.FINE_APPROVED, fine_amount=None)
    for on, phrase in ((False, "There is no fine to pay."), (True, "There is no fine to pay (suspension only).")):
        settings.SUSPENSION_ENABLED = on
        with pytest.raises(services.WorkflowError) as e:
            services.record_payment(v, date.today())
        assert str(e.value) == phrase
    for on, phrase in ((False, "No fine will be imposed and this matter is closed."), (True, "No fine or suspension will be imposed and this matter is closed.")):
        settings.SUSPENSION_ENABLED = on
        m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=1), results_status="submitted")
        w = Violation.objects.create(owner=Owner.objects.create(name="W", property_address=f"{int(on) + 40} Waive St", email="w@example.com"), vtype=t,
                                     description="d", cure_required="c", status=S.LEDGER_PENDING, committee_meeting=m, committee_outcome="confirm",
                                     fine_amount=100, committee_votes_for=3, committee_votes_against=0)
        mail.outbox.clear()
        services.board_review_item(w, None, "modify", final_outcome="waive")
        assert phrase in mail.outbox[0].body


def test_the_letters_still_have_their_current_wording_for_now():
    """NOT a wish: this records which suspension phrases the three letters still contain, so they can be removed when the attorney's final
    wording arrives (Part B). Unconditional phrases are always printed; conditional ones only print if a case has suspension data."""
    o = Owner.objects.create(name="Pat", property_address="9 Phrase Way", email="p@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.get_or_create(name="Shed")[0], description="d", cure_required="c",
                                 status=S.HEARING_SCHEDULED, proposed_fine=100, fine_amount=100, committee_votes_for=3, committee_votes_against=0,
                                 board_meeting_date=date.today(), hearing_date=timezone.now() + timedelta(days=20), committee_cure_by=date.today())

    def text(kind):
        n = Notice.objects.create(violation=v, kind=kind)
        return " ".join(" ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(render_notice_pdf(n, "owner"))).pages).split())
    nov, hearing, decision = text("violation"), text("hearing"), text("fine")
    assert "and/or suspending your right to use certain Common Areas and facilities" in nov and "A fine or suspension may be imposed only after notice" in nov
    assert "confirm or reject the fine and/or suspension that the Board levied" in hearing
    assert "the fine and/or suspension, if confirmed by the Committee" in hearing and "If the Committee rejects the fine and/or suspension" in hearing
    assert "or violate the Association's suspension order" in decision and "enforce the suspension" in decision
    assert "no fine or suspension will be imposed" in decision          # only printed when the Committee set a cure-by date
