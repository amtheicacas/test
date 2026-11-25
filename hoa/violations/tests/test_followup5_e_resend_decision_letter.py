"""Follow-up after review pass 4 (owner's answer `resend-button`, then "go"): a decision letter (the Notice of committee decision) that could not be
created after the decision was saved can now be sent again with one button on the case, and such cases are listed on the Board / manager home screen.
Sending the letter is a stage move, not a decision, so it is open to everyone who may work the case, including on their own household's case.
Nothing is sent outside: Django's in-memory outbox; the print service is switched off (MAIL_PROVIDER manual)."""
from datetime import date, timedelta

import pytest
from django.core import mail
from django.urls import reverse

from violations import services
from violations.models import Event, MailPiece, Notice, Profile, Violation
from violations.tests.review4_support import board, make_case  # noqa: F401  (board is a fixture)

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db


def imposed(n=1, status=S.FINE_APPROVED, **kw):
    extra = dict(fine_amount=100, proposed_fine=100, committee_outcome="confirm", committee_findings="It was observed.", committee_votes_for=3,
                 committee_votes_against=0, ledger_posted_on=TODAY, ledger_reference="V-1", fine_due_on=TODAY + timedelta(days=35))
    if status == S.FINE_REJECTED:
        extra = dict(committee_outcome="reject", committee_findings="It was observed.", committee_votes_for=0, committee_votes_against=3)
    extra.update(kw)
    return make_case(status=status, n=n, **extra)


def fine_notices(v):
    return Notice.objects.filter(violation=v, kind=Notice.Kind.FINE)


# ---- when is a decision letter "missing"? ----
def test_F5_an_imposed_or_rejected_case_with_no_decision_letter_is_missing_one():
    a, b = imposed(1), imposed(2, S.FINE_REJECTED)
    assert services.decision_letter_missing(a) and services.decision_letter_missing(b)
    assert {v.pk for v in services.decision_letters_missing()} == {a.pk, b.pk}


def test_F5_a_case_that_got_its_letter_is_not_listed():
    v = imposed()
    services.send_notice(v, Notice.Kind.FINE)
    assert not services.decision_letter_missing(v) and list(services.decision_letters_missing()) == []


def test_F5_an_empty_half_made_notice_does_not_count_as_a_letter():
    v = imposed()
    Notice.objects.create(violation=v, kind=Notice.Kind.FINE)             # what a failure half way through leaves behind
    assert services.decision_letter_missing(v) and [x.pk for x in services.decision_letters_missing()] == [v.pk]


@pytest.mark.parametrize("trace", ["emailed", "queued", "mailed", "piece", "email_error"])
def test_F5_any_trace_that_the_letter_was_sent_or_is_waiting_counts(trace):
    v = imposed()
    n = Notice.objects.create(violation=v, kind=Notice.Kind.FINE)
    if trace == "emailed":
        n.emailed_at = services.timezone.now()
    elif trace == "queued":
        n.mail_queued_on = services.timezone.now()
    elif trace == "mailed":
        n.mailed_on = TODAY
    elif trace == "email_error":
        n.email_error = "SMTP down"
    n.save()
    if trace == "piece":
        MailPiece.objects.create(notice=n, label="owner mailing", service="certified", by_hand=True)
    assert not services.decision_letter_missing(v)


@pytest.mark.parametrize("status", [S.NOTICE_SENT, S.PENDING_BOARD, S.HEARING_SCHEDULED, S.LEDGER_PENDING, S.WAIVED, S.DISMISSED, S.CLOSED])
def test_F5_other_stages_never_need_one(status):
    assert not services.decision_letter_missing(make_case(status=status))


# ---- sending it again ----
def test_F5_sending_it_again_emails_the_owner_once_and_writes_the_history(settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    n = services.resend_decision_letter(v, None)
    assert fine_notices(v).count() == 1 and n.pk == fine_notices(v).get().pk
    assert [m.to for m in mail.outbox] == [["p1@example.com"]] and "decision" in mail.outbox[0].subject.lower()
    assert not services.decision_letter_missing(v)
    assert Event.objects.filter(violation=v, text__icontains="sent again").exists()


def test_F5_a_second_press_sends_nothing_more(settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    services.resend_decision_letter(v, None)
    sent = len(mail.outbox)
    with pytest.raises(services.WorkflowError, match="already has"):
        services.resend_decision_letter(v, None)
    assert len(mail.outbox) == sent and fine_notices(v).count() == 1


def test_F5_a_half_made_notice_does_not_stop_a_real_one_being_sent(settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    Notice.objects.create(violation=v, kind=Notice.Kind.FINE)
    services.resend_decision_letter(v, None)
    assert [n for n in fine_notices(v) if n.emailed_at] and not services.decision_letter_missing(v)


def test_F5_a_rejected_case_can_be_sent_its_letter_too(settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed(status=S.FINE_REJECTED)
    services.resend_decision_letter(v, None)
    assert len(mail.outbox) == 1


@pytest.mark.parametrize("status", [S.NOTICE_SENT, S.LEDGER_PENDING, S.HEARING_SCHEDULED, S.WAIVED, S.DISMISSED])
def test_F5_it_is_refused_where_no_decision_letter_belongs(status):
    with pytest.raises(services.WorkflowError):
        services.resend_decision_letter(make_case(status=status), None)
    assert not mail.outbox


def test_F5_a_failure_is_reported_in_the_history_and_leaves_no_half_notice(monkeypatch):
    v = imposed()

    def broken(*a, **kw):
        raise RuntimeError("the letter could not be built")
    monkeypatch.setattr(services, "send_notice", broken)
    with pytest.raises(services.DecisionLetterFailed, match="NOT been sent"):
        services.resend_decision_letter(v, None)
    assert Event.objects.filter(violation=v, text__startswith="WARNING").exists() and services.decision_letter_missing(v)


def test_F5_your_own_households_letter_may_be_sent_it_is_not_a_decision(settings, django_user_model):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    u = django_user_model.objects.create_user("own", "own@example.com", "pw")
    Profile.objects.create(user=u, role="board", owner=v.owner)
    assert services.own_household(u, v)
    services.resend_decision_letter(v, u)
    assert len(mail.outbox) == 1


# ---- on the screens ----
def test_F5_the_case_page_offers_the_button_only_while_the_letter_is_missing(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    url = reverse("violation_detail", args=[v.pk])
    page = board.get(url).content.decode()
    assert "Send the decision letter again" in page and f"/violations/{v.pk}/resend_decision_letter/" in page
    services.resend_decision_letter(v, None)
    assert "Send the decision letter again" not in board.get(url).content.decode()


def test_F5_pressing_it_in_the_browser_sends_once_and_says_so_even_when_pressed_twice(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    r = board.post(f"/violations/{v.pk}/resend_decision_letter/", follow=True)
    assert "decision letter" in r.content.decode().lower() and len(mail.outbox) == 1
    r = board.post(f"/violations/{v.pk}/resend_decision_letter/", follow=True)
    assert "already has" in r.content.decode() and len(mail.outbox) == 1


def test_F5_the_home_screen_lists_the_cases_and_the_line_goes_when_none_are_left(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    page = board.get(reverse("dashboard")).content.decode()
    assert "Decision letters not sent" in page and v.owner.property_address in page and f"/violations/{v.pk}/" in page
    services.resend_decision_letter(v, None)
    assert "Decision letters not sent" not in board.get(reverse("dashboard")).content.decode()


def test_F5_an_owner_cannot_press_it(client, django_user_model, settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    u = django_user_model.objects.create_user("ow", "ow@example.com", "pw")
    Profile.objects.create(user=u, role="homeowner", owner=v.owner)
    client.force_login(u)
    r = client.post(f"/violations/{v.pk}/resend_decision_letter/")
    assert r.status_code in (302, 403, 404) and not mail.outbox and fine_notices(v).count() == 0
