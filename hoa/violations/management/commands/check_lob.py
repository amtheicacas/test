from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from violations import mailing


class Command(BaseCommand):
    help = ("Check the print-service (Lob) settings WITHOUT sending anything: which kind of key is set, how the return address will print, and "
            "whether a staging site is safe. --ping also asks Lob (read-only, free) whether it accepts the key. Never prints the key.")

    def add_arguments(self, p):
        p.add_argument("--ping", action="store_true", help="ask Lob a read-only question to see whether it accepts the key")

    def handle(self, *a, **o):
        out, bad = self.stdout.write, []
        provider, mode = settings.MAIL_PROVIDER, mailing.lob_key_mode()
        out(f"MAIL_PROVIDER is '{provider}'" + ("" if provider == "lob" else ": nothing is sent to Lob (letters are printed and mailed by hand)."))
        if mode == "missing":
            out("LOB_API_KEY: not set.")
            if provider == "lob":
                bad.append("MAIL_PROVIDER is lob but LOB_API_KEY is not set.")
        else:
            kind = {"test": "a TEST key (test_...): Lob accepts letters and shows them in its dashboard, but NOTHING IS MAILED and nothing is charged.",
                    "live": "a LIVE key (live_...): letters are REALLY printed and mailed and REAL postage is charged.",
                    "unknown": "not recognised (it should start with test_ or live_)."}[mode]
            out(f"LOB_API_KEY: {len(settings.LOB_API_KEY.strip())} characters, {kind}")
            if mode == "unknown":
                bad.append("The Lob key does not start with test_ or live_: check that the whole key was copied.")
        live_site = settings.PRODUCTION and not settings.STAGING
        if not live_site and mode not in ("test", "missing"):
            bad.append("This is not the live site (it is staging or a test copy), so it may only use a Lob TEST key. Replace the key.")
        if live_site and provider == "lob" and mode == "test":
            bad.append("This is the live site but the key is a TEST key: letters would be accepted by Lob and not mailed.")
        full = settings.HOA_RETURN_NAME or settings.HOA_NAME
        out("Return address as it will print on the envelope:")
        out(f"    {mailing.return_name()}" + ("   (cut to Lob's 40-character limit; set HOA_RETURN_NAME to choose the wording)" if mailing.return_name() != full else ""))
        out(f"    {settings.HOA_RETURN_LINE1 or '(HOA_RETURN_LINE1 is not set)'}")
        out(f"    {settings.HOA_RETURN_CITY}, {settings.HOA_RETURN_STATE} {settings.HOA_RETURN_ZIP or '(HOA_RETURN_ZIP is not set)'}")
        if provider == "lob" and not (settings.HOA_RETURN_LINE1 and settings.HOA_RETURN_ZIP):
            bad.append("The return address is incomplete: set HOA_RETURN_LINE1 and HOA_RETURN_ZIP (city and state default to Sarasota, FL). "
                       "On a test site any made-up address will do.")
        if o["ping"]:
            if mode == "missing":
                bad.append("Cannot ask Lob without a key.")
            else:
                try:
                    status = mailing.lob_ping()
                except Exception as e:      # noqa: BLE001  (no network, timeout...)
                    bad.append(f"Could not reach Lob: {e}")
                else:
                    out(f"Lob answered HTTP {status}: " + ("it accepts the key." if status == 200 else "it REFUSED the key (check it was copied whole)." if status == 401 else "unexpected answer."))
                    if status != 200:
                        bad.append(f"Lob did not accept the key (HTTP {status}).")
        if bad:
            raise CommandError("Problems found:\n  - " + "\n  - ".join(bad))
        out("All checks passed. (Nothing was sent to Lob" + (" except the read-only key check" if o["ping"] else "") + ".)")
