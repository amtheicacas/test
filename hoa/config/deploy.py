"""Helpers used by settings.py for hosting: reading DATABASE_URL and refusing to start a production site with unsafe settings.

Kept free of Django imports so it can be tested on its own."""
from urllib.parse import parse_qs, unquote, urlsplit

DEV_SECRET = "dev-only-insecure-key"


def database_from_url(url, conn_max_age=60, ssl_require=False):
    """'postgres://user:pass@host:5432/dbname?sslmode=require' -> a Django DATABASES entry. Only PostgreSQL is accepted."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must start with postgres:// or postgresql://")
    name = unquote(parts.path.lstrip("/"))
    if not parts.hostname or not name:
        raise ValueError("DATABASE_URL needs a host and a database name, e.g. postgres://user:password@host:5432/dbname")
    cfg = {"ENGINE": "django.db.backends.postgresql", "NAME": name, "USER": unquote(parts.username or ""),
           "PASSWORD": unquote(parts.password or ""), "HOST": parts.hostname, "PORT": str(parts.port or ""),
           "CONN_MAX_AGE": conn_max_age, "CONN_HEALTH_CHECKS": True, "OPTIONS": {}}
    sslmode = (parse_qs(parts.query).get("sslmode") or [""])[0]
    if sslmode:
        cfg["OPTIONS"]["sslmode"] = sslmode
    elif ssl_require:
        cfg["OPTIONS"]["sslmode"] = "require"
    return cfg


def production_problems(*, secret_key, allowed_hosts, site_url, database_url, email_host, from_email, mfa_roles, file_storage=True, vantaca_url="",
                        mail_provider="manual", lob_key="", staging=False):
    """Plain-language list of what is wrong for a live site (empty list = fine)."""
    out = []
    if not secret_key or secret_key == DEV_SECRET or secret_key.startswith("django-insecure") or len(secret_key) < 32:
        out.append("SECRET_KEY must be set to a long random value (32+ characters), different from your test site's.")
    if not allowed_hosts:
        out.append("ALLOWED_HOSTS must list the real website address(es), e.g. ALLOWED_HOSTS=hoa.example.org")
    elif "*" in allowed_hosts:
        out.append("ALLOWED_HOSTS must not contain *: list the real website address(es).")
    if not site_url.startswith("https://"):
        out.append("SITE_URL must be the real website address starting with https:// (every emailed link is built from it).")
    if not database_url:
        out.append("DATABASE_URL must be set to the PostgreSQL database address.")
    if not email_host:
        out.append("EMAIL_HOST (and the other EMAIL_ settings) must be set: without them sign-in links would be printed in the "
                   "server log instead of being emailed.")
    if not from_email or from_email.lower().endswith("@example.org"):
        out.append("DEFAULT_FROM_EMAIL must be a real address on the association's domain.")
    if not file_storage:
        out.append("File storage must be set: STORAGE_BUCKET (a private bucket) or MEDIA_ROOT (a persistent disk). Otherwise the saved copies "
                   "of every letter sent, the photos and the minutes would be lost each time the site is updated.")
    if vantaca_url and not vantaca_url.strip().lower().startswith("https://"):
        out.append("VANTACA_BASE_URL must start with https:// (the Vantaca password is sent with every request).")
    if mail_provider == "lob":
        key = (lob_key or "").strip()
        if not key:
            out.append("MAIL_PROVIDER=lob needs LOB_API_KEY (the secret key from your Lob account).")
        elif staging and not key.startswith("test_"):
            out.append("This is a staging site (STAGING=1), so LOB_API_KEY must be a Lob TEST key (it starts with test_), never a live key: "
                       "a live key would print and mail real letters and charge real postage.")
        elif not staging and key.startswith("test_"):
            out.append("LOB_API_KEY is a Lob TEST key (test_...). Lob would accept the letters but MAIL NOTHING, while this site showed them as "
                       "mailed. Use the live key (live_...) on the live site.")
    if not mfa_roles:
        out.append("Two-step login is switched off (MFA_REQUIRED_ROLES is empty or 'none'). Remove that setting on the live site.")
    return out


def explain(problems):
    return ("The site will not start with these settings:\n  - " + "\n  - ".join(problems) +
            "\nOn your own PC, add the line DJANGO_DEBUG=1 to hoa/local.env (start-site.bat already does this). "
            "On the live host, set these in its Environment settings.")
