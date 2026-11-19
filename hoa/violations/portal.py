"""The owner portal's front page: a set of tiles, one per module (violations today; requests, inquiries... later).
To add a module, add a function that returns a Tile and list it in OWNER_MODULES."""
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.urls import reverse

from .models import Violation

S = Violation.Status


@dataclass
class Tile:
    key: str
    title: str
    icon: str                 # the big symbol on the tile
    blurb: str
    url: str
    external: bool = False
    state: str = ""           # green / yellow / orange / red / gray / warn: colors the status line and icon
    status_icon: str = ""
    status_text: str = ""
    detail: str = ""


# worst first: what the owner's open violations say about where things stand
_SEVERITY = [
    ("red", "⚖", "With the Compliance Committee", (S.HEARING_SCHEDULED, S.LEDGER_PENDING, S.FINE_APPROVED, S.REFERRED_LEGAL)),
    ("orange", "\U0001f5f3", "With the Board", (S.PENDING_BOARD, S.HEARING_NEEDED)),
    ("yellow", "⏳", "In cure period", (S.NOTICE_SENT,)),
    ("gray", "⏸", "On hold", (S.ON_HOLD,)),
]


def violations_tile(user, owner):
    open_cases = list(owner.violations.exclude(status=S.DRAFT).filter(status__in=Violation.OPEN)) if owner else []
    t = Tile("violations", "Violations", "\U0001f4cb", "Notices, letters and your responses.", reverse("my_violations"))
    if not open_cases:
        t.state, t.status_icon, t.status_text, t.detail = "green", "✔", "All clear", "No open violations."
        return t
    for state, icon, text, statuses in _SEVERITY:
        if any(v.status in statuses for v in open_cases):
            t.state, t.status_icon, t.status_text = state, icon, text
            break
    n = len(open_cases)
    t.detail = f"{n} open"
    due = [v.cure_deadline for v in open_cases if v.status == S.NOTICE_SENT and v.cure_deadline]
    if due:
        nxt = min(due)
        left = (nxt - date.today()).days
        t.detail += f" · next cure date {nxt:%b} {nxt.day}"
        if left <= 3:
            t.state, t.status_icon = "red" if left < 0 else "orange", "⚠"
            t.status_text = "Past the cure date" if left < 0 else ("Due today" if left == 0 else f"Due in {left} day{'s' if left != 1 else ''}")
    return t


def account_tile(user, owner):
    t = Tile("account", "Account details", "\U0001f464", "The name, address and email we have for you.", reverse("my_household"))
    if owner is None:
        return t
    t.detail = owner.property_address
    if not owner.all_emails:
        t.state, t.status_icon, t.status_text = "warn", "⚠", "No email on file: contact the management office"
    elif owner.tenant_lease_ended:
        t.state, t.status_icon, t.status_text = "warn", "⚠", "Tenant lease has ended: contact the management office"
    else:
        t.state, t.status_icon, t.status_text = "green", "✔", "Email on file"
    return t


def documents_tile(user, owner):
    return Tile("documents", "HOA documents", "\U0001f4c4", "The Declaration, Community Standards and more.", settings.HOA_DOCUMENTS_URL,
                external=True, state="gray", status_icon="↗", status_text="Opens in a new tab")


def signin_tile(user, owner):
    return Tile("signin", "Sign-in options", "\U0001f511", "Your emailed sign-in link and an optional password.", reverse("my_password"),
                state="gray", status_icon="✉", status_text="Sign in with an emailed link")


OWNER_MODULES = [violations_tile, account_tile, documents_tile, signin_tile]


def owner_tiles(user, owner):
    return [m(user, owner) for m in OWNER_MODULES]
