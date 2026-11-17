"""Post an approved fine to the owner's ledger in Vantaca.

IMPORTANT: this is configurable rather than hard-wired because Vantaca's ledger/charge endpoint and its payload are not public.
Get the endpoint, required fields, charge code and a test environment from Vantaca vendor support, then set
VANTACA_LEDGER_POST_PATH, VANTACA_FINE_CHARGE_CODE and (if their fields differ) VANTACA_LEDGER_BODY. Until then
posting is manual: staff post the fine in Vantaca and record the reference here. It has not been run against a live account."""
import json
from string import Template

from django.conf import settings

from . import vantaca


class LedgerError(Exception):
    pass


def configured():
    return bool(settings.VANTACA_LEDGER_POST_PATH and settings.VANTACA_FINE_CHARGE_CODE and settings.VANTACA_BASE_URL)


def reference_for(v):
    return f"HOA-VIOL-{v.pk}"  # stable per case, so a retry is recognizable to Vantaca/staff


def build_body(v, today):
    esc = lambda x: json.dumps(str(x))[1:-1]  # JSON-escape without surrounding quotes
    body = Template(settings.VANTACA_LEDGER_BODY).safe_substitute(
        account_id=esc(v.owner.external_id), amount=f"{v.fine_amount:.2f}", date=today.isoformat(),
        description=esc(f"HOA fine - {v.vtype.name} (case #{v.pk})"), reference=esc(reference_for(v)),
        charge_code=esc(settings.VANTACA_FINE_CHARGE_CODE))
    return json.loads(body)


def post_fine(v, today):
    """Post v.fine_amount to the owner's Vantaca account. Returns the transaction reference."""
    if not configured():
        raise LedgerError("Posting to Vantaca isn't set up yet. Post the fine in Vantaca and record it here.")
    if not v.owner.external_id:
        raise LedgerError("This owner has no Vantaca account ID (sync owners from Vantaca first), so the fine can't be posted automatically.")
    try:
        reply = vantaca.api_request("POST", settings.VANTACA_LEDGER_POST_PATH, build_body(v, today))
    except Exception as e:
        raise LedgerError(f"Vantaca did not accept the posting ({e}). Nothing was posted; you can retry or post it by hand.")
    ref = reply.get(settings.VANTACA_LEDGER_REF_KEY) if isinstance(reply, dict) else None
    return str(ref) if ref else reference_for(v)
