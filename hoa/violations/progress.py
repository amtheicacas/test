"""The progress tracker. One internal status per case (see Violation.Status); this module turns it into

* the Association's six procedure steps for owners, each with a date and, for the current step, one plain sentence on what it means
  and what to do, and
* the same six steps for the Board / Manager with sub-steps (done / current / not yet / bypassed) and their dates.

All wording lives here, so renaming a step or sentence is a one-place edit."""
from datetime import date

from django.utils import timezone

# The six steps of the Association's violation procedure, in its own words.
STAGES = [
    ("notice", "Notice issued", "\u2709"),
    ("cure", "Cure period", "\u23f3"),
    ("board", "Fine issued by Board", "\U0001f5f3"),
    ("hearing", "Violation sent to Compliance Committee / Homeowner Hearing", "\U0001f465"),
    ("imposed", "Fine Imposed & Posted to Ledger", "$"),
    ("closed", "Closed", "\u2714"),
]


def _d(x):
    """'Dec 3, 2026' (the '%-d' strftime flag does not work on Windows)."""
    if x is None:
        return ""
    if hasattr(x, "hour"):
        x = timezone.localtime(x).date() if timezone.is_aware(x) else x.date()
    return f"{x:%b} {x.day}, {x.year}"


def _status():
    from .models import Violation
    return Violation.Status


def _current(v):
    S = _status()
    st = v.held_from if v.status == S.ON_HOLD and v.held_from else v.status
    return {S.DRAFT: -1, S.NOTICE_SENT: 1, S.PENDING_BOARD: 2, S.HEARING_NEEDED: 2, S.HEARING_SCHEDULED: 3, S.LEDGER_PENDING: 4,
            S.FINE_APPROVED: 4, S.REFERRED_LEGAL: 4}.get(st, 5)


def _evidence(v):
    """Did the case actually go through the Board / Committee steps? (A direct referral or an early cure skips them.)"""
    return {2: bool(v.board_votes_for is not None or v.board_meeting_date or v.proposed_fine or v.committee_meeting_id),
            3: bool(v.committee_outcome or v.committee_votes_for is not None or v.committee_meeting_id),
            4: bool(v.fine_due_on or v.ledger_posted_on or v.suspension or (v.fine_amount and v.status in ("fine_approved", "closed")))}


def stage_dates(v):
    first = next((n for n in v.notices.all() if n.kind == "violation"), None)
    return {
        "notice": (first.created_at if first else v.created_at) if v.status != "draft" else None,
        "cure": v.cure_deadline,
        "board": v.board_meeting_date,
        "hearing": v.hearing_date,
        "imposed": v.ledger_posted_on or v.fine_due_on,
        "closed": v.cured_on or v.fine_paid_on,
    }


def stages(v):
    """[{key, title, icon, state, date, text}] for the five steps. state: done / current / todo / skipped."""
    S = _status()
    cur = _current(v)
    legal = v.status == S.REFERRED_LEGAL
    ended = v.status in (S.CURED, S.CLOSED, S.DISMISSED, S.FINE_REJECTED, S.WAIVED)
    ev = _evidence(v)
    dates = stage_dates(v)
    out = []
    for i, (key, title, icon) in enumerate(STAGES):
        if v.status == S.DRAFT:
            state = "todo"
        elif i == 1 and v.non_curable:
            state = "skipped"                      # a point-in-time violation has no cure period
        elif i < cur:
            state = "skipped" if (ended or legal) and i in ev and not ev[i] else "done"
        elif i == cur and not ended and not legal:
            state = "current"
        elif legal and i == cur:
            state = "done" if ev.get(i, True) else "skipped"
        elif cur == 5:
            state = "done"
        else:
            state = "todo"
        if legal and i == 5:
            state = "todo"
        out.append({"key": key, "title": title, "icon": "↷" if state == "skipped" else icon, "state": state,
                    "date": dates[key] if state in ("done", "current") else None, "text": ""})
    if legal:   # referred to the attorney: its own step, just before Closed
        out.insert(len(out) - 1, {"key": "legal", "title": "Referred to the attorney", "icon": "\u2696", "state": "current",
                                  "date": v.legal_sent_on or v.legal_referred_on, "text": ""})
    cur_stage = next((x for x in out if x["state"] == "current"), None)
    if cur_stage is None and ended:
        cur_stage = out[-1]
        cur_stage["date"] = dates["closed"]
    if cur_stage is not None:
        cur_stage["text"] = owner_sentence(v)
    return out


def owner_sentence(v):
    """One plain sentence for the owner about where things stand and what (if anything) to do."""
    S = _status()
    st = v.status
    held = ""
    if st == S.ON_HOLD:
        held = f" This case is on hold until {_d(v.hold_until)}." if v.hold_until else " This case is on hold."
        st = v.held_from or S.NOTICE_SENT
    if st == S.NOTICE_SENT:
        if v.reinspect_on:
            return "You told us this is fixed. We will verify it at our next inspection." + held
        when = "immediately" if v.total_cure_days == 0 else f"by {_d(v.cure_deadline)}"
        if v.non_curable:
            return "This is a one-time event, so there is no cure period. The Board will review it." + held
        return f"Please correct this {when}. You can reply, upload a photo of the fix, or ask for more time." + held
    if st == S.PENDING_BOARD:
        return "The cure time has ended. The Board is reviewing this violation." + held
    if st == S.HEARING_NEEDED:
        return "The Board has issued a fine, which is subject to a Compliance Committee hearing. A hearing date is being scheduled." + held
    if st == S.HEARING_SCHEDULED:
        return (f"A hearing is set for {_d(v.hearing_date)}. Your hearing notice explains how to attend." if v.hearing_date
                else "A hearing is being scheduled.") + held
    if st == S.LEDGER_PENDING:
        return "The Committee has decided your case. The Board is finalizing the decision and you will receive a letter."
    if st == S.FINE_APPROVED:
        parts = []
        if v.fine_amount:
            parts.append(f"A fine of ${v.fine_amount:,.2f} was imposed" + (f", due {_d(v.fine_due_on)}" if v.fine_due_on else "") + ".")
        else:
            parts.append("A decision was made on this violation.")
        return " ".join(parts) + (" Please see your decision letter." if v.fine_amount else "")
    if st == S.FINE_REJECTED:
        return "The Committee did not approve a fine. This matter is closed."
    if st == S.WAIVED:
        return "The Committee waived this violation. This matter is closed."
    if st == S.CURED:
        return "This violation was corrected. Thank you."
    if st == S.CLOSED:
        return "This violation was corrected and any fine paid. This matter is closed."
    if st == S.DISMISSED:
        return "This violation was dismissed. No further action is needed."
    if st == S.REFERRED_LEGAL:
        return "This matter has been referred to the Association's attorney."
    return ""


# ---------------- Board / Manager view: sub-steps ----------------
def _sub(label, state, when=None, note=""):
    return {"label": label, "state": state, "date": _d(when) if when else "", "note": note}


def substeps(v):
    """{stage key: [sub-step, ...]} for the Board / Manager. state: done / current / todo / skipped."""
    S = _status()
    st = v.held_from if v.status == S.ON_HOLD and v.held_from else v.status
    cur = _current(v)
    notices = list(v.notices.all())
    kinds = {n.kind: n for n in notices}
    first = kinds.get("violation")
    responses = [r for r in v.responses.all() if r.from_resident]
    out = {}

    # notice sent
    n = first
    mail = "todo"
    mail_note = ""
    if n is not None:
        if n.mailed_on:
            mail, mail_note = "done", n.get_method_display() or "mailed"
        elif n.mail_cancelled:
            mail, mail_note = "skipped", "cancelled: corrected first"
        elif n.mail_held:
            mail, mail_note = "current", f"held until {_d(n.mail_hold_until)}"
    out["notice"] = [
        _sub("Notice issued", "done" if v.status != S.DRAFT else "todo", first.created_at if first else None),
        _sub("Emailed", "done" if n is not None and n.emailed_at else ("current" if n is not None else "todo"),
             n.emailed_at if n is not None else None, (n.emailed_to if n is not None and n.emailed_at else "")),
        _sub("US Mail copy", mail, n.mailed_on if n is not None else None, mail_note),
    ]
    # time to fix it
    cure = []
    if v.non_curable:
        cure.append(_sub("No cure period (point in time)", "skipped"))
    else:
        done = cur > 1
        cure.append(_sub("Cure period" + (" (restarted after a hold)" if v.off_hold_on else ""), "done" if done else ("current" if cur == 1 else "todo"),
                         v.cure_deadline, "ends" if not done else "ended"))
        cure.append(_sub("Owner replied", "done" if responses else "todo", responses[-1].created_at if responses else None,
                         f"{len(responses)} message{'s' if len(responses) != 1 else ''}" if responses else ""))
        if v.extended_days or "extension" in kinds:
            cure.append(_sub("Extension granted", "done", None, f"{v.extended_days} day{'s' if v.extended_days != 1 else ''}"))
        if v.reinspect_on:
            cure.append(_sub("Re-inspect", "current", v.reinspect_on, f"{v.reinspect_age} days waiting"))
        if v.status == S.ON_HOLD:
            cure.append(_sub("On hold", "current", v.hold_until, "until"))
        elif v.off_hold_on:
            cure.append(_sub("Hold ended", "done", v.off_hold_on))
    out["cure"] = cure
    # with the Board
    voted = bool(v.board_meeting_date)
    out["board"] = [
        _sub("Waiting for Board vote", "current" if st == S.PENDING_BOARD else ("done" if cur > 2 or voted else "todo")),
        _sub("Board voted", "done" if voted else "todo", v.board_meeting_date,
             "fine $" + f"{v.proposed_fine:,.2f}" if voted and v.proposed_fine else ""),
        _sub("Assigned to a committee meeting", "current" if st == S.HEARING_NEEDED else ("done" if v.committee_meeting_id else "todo"),
             v.committee_meeting.starts_at if v.committee_meeting_id else None),
    ]
    # Compliance Committee / hearing
    hearing_notice = kinds.get("hearing")
    held = bool(v.hearing_date and v.hearing_date <= timezone.now())
    out["hearing"] = [
        _sub("Hearing notice sent (certified)", "done" if hearing_notice else "todo", hearing_notice.created_at if hearing_notice else None),
        _sub("Hearing held", "done" if held else "todo", v.hearing_date),
        _sub("Committee decision recorded", "done" if v.committee_outcome else "todo", None, v.get_committee_outcome_display() if v.committee_outcome else ""),
        _sub("Meeting results submitted to the Board", "done" if v.committee_outcome and st != S.HEARING_SCHEDULED else "todo"),
    ]
    # fine imposed and posted
    ledger_review = st == S.LEDGER_PENDING
    decision = kinds.get("fine")
    imp = [_sub("Board review of the Committee's decision", "current" if ledger_review else ("done" if v.board_reviewed_on or (cur > 4 and v.committee_outcome) else "todo"),
                v.board_reviewed_on, "modified" if v.board_modified else "")]
    imp.append(_sub("Decision letter sent (certified)", "done" if decision else "todo", decision.created_at if decision else None))
    if v.fine_amount:
        imp.append(_sub("Posted to ledger", "done" if v.ledger_posted_on else "todo", v.ledger_posted_on, f"${v.fine_amount:,.2f}"))
        imp.append(_sub("Payment received", "done" if v.fine_paid_on else ("current" if st == S.FINE_APPROVED else "todo"),
                        v.fine_paid_on, "due " + _d(v.fine_due_on) if v.fine_due_on and not v.fine_paid_on else ""))
    out["imposed"] = imp
    # closed
    ended = v.status in (S.CURED, S.CLOSED, S.DISMISSED, S.FINE_REJECTED, S.WAIVED)
    how = {S.CURED: "cured", S.CLOSED: "cured and paid", S.DISMISSED: "dismissed", S.FINE_REJECTED: "fine rejected", S.WAIVED: "waived"}.get(v.status, "")
    out["closed"] = [_sub("Closed", "done" if ended else "todo", v.cured_on if ended else None, how)]
    if v.status == S.REFERRED_LEGAL:
        out["legal"] = [
            _sub("Referred to the attorney", "done", v.legal_referred_on, "direct referral" if v.legal_path == "direct" else "after the fine"),
            _sub("Email to the attorney", "done" if v.legal_sent_on else "current", v.legal_sent_on, v.legal_sent_to if v.legal_sent_on else "not sent yet"),
        ]
    return out


def staff_stages(v):
    """stages() with each step's sub-steps attached."""
    subs = substeps(v)
    steps = stages(v)
    for st_ in steps:
        st_["subs"] = subs.get(st_["key"], [])
    return steps
