import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


DEBUG = env("DJANGO_DEBUG", "1") == "1"
SECRET_KEY = env("SECRET_KEY") or ("dev-only-insecure-key" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("Set SECRET_KEY when DJANGO_DEBUG=0")
ALLOWED_HOSTS = env("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "axes",
    "violations",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "violations.middleware.RequireTwoStep",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "violations.context.hoa",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---- Sign-in security -----------------------------------------------------
AUTHENTICATION_BACKENDS = ["axes.backends.AxesStandaloneBackend", "django.contrib.auth.backends.ModelBackend"]
AXES_FAILURE_LIMIT = int(env("LOGIN_FAILURE_LIMIT", "5"))      # lock after 5 bad passwords...
AXES_COOLOFF_TIME = 1                                          # ...for 1 hour
AXES_LOCKOUT_PARAMETERS = [["ip_address", "username"]]
AXES_RESET_ON_SUCCESS = True
# Roles that MUST use an authenticator app (TOTP) after their password. Homeowners may opt in.
MFA_REQUIRED_ROLES = tuple(r for r in env("MFA_REQUIRED_ROLES", "manager,board,committee").split(",") if r)
OTP_TOTP_ISSUER = env("OTP_TOTP_ISSUER", "Heron Landing HOA")
SESSION_COOKIE_AGE = int(env("SESSION_HOURS", "12")) * 3600
PASSWORD_RESET_TIMEOUT = 3 * 24 * 3600                          # invitation / reset links last 3 days
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "media")))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024

# Email: console by default so nothing is sent until SMTP is configured.
if env("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST")
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env("EMAIL_USE_TLS", "1") == "1"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "violations@example.org")

if not DEBUG:
    SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")   # behind Azure / a load balancer
    SECURE_HSTS_SECONDS = 31536000

# ---- HOA configuration --------------------------------------------------
HOA_NAME = env("HOA_NAME", "Heron Landing Homeowners’ Association, Inc.")
HOA_MAILING_ADDRESS = env("HOA_MAILING_ADDRESS", "c/o Sunstate Management, Sarasota, FL")
MANAGER_NAME = env("MANAGER_NAME", "Erick Jacobsen")
MANAGER_EMAIL = env("MANAGER_EMAIL", "Erick@sunstatemanagement.com")
OFFICE_PHONE = env("OFFICE_PHONE", "941-870-4920")
SITE_URL = env("SITE_URL", "http://localhost:8000").rstrip("/")
# Fining Resolution / Fla. Stat. 720.305(2): up to $100 per day for a continuing violation, up to $1,000 total.
FINE_MAX_PER_DAY = int(env("FINE_MAX_PER_DAY", "100"))
FINE_AGGREGATE_CAP = int(env("FINE_AGGREGATE_CAP", "1000"))
FINE_PAYMENT_DAYS = int(env("FINE_PAYMENT_DAYS", "5"))        # fine due this many days after the imposition letter
ATTORNEY_NOTICE_DAYS = int(env("ATTORNEY_NOTICE_DAYS", "7"))  # owner must tell us this many days before the hearing
REPLY_TARGET_HOURS = int(env("REPLY_TARGET_HOURS", "48"))     # procedure promises a reply within 48-72 hours
# Fla. Stat. 720.305(2)(b): at least 14 days' notice before the committee hearing.
HEARING_NOTICE_DAYS = int(env("HEARING_NOTICE_DAYS", "14"))

# ---- Vantaca sync (all optional) ---------------------------------------
VANTACA_BASE_URL = env("VANTACA_BASE_URL", "")            # e.g. the API host Vantaca gives you
VANTACA_USER = env("VANTACA_USER", "")                    # Basic-auth credentials issued by Vantaca
VANTACA_PASSWORD = env("VANTACA_PASSWORD", "")
VANTACA_OWNERS_PATH = env("VANTACA_OWNERS_PATH", "")      # read endpoint that lists homeowners, from the API docs

# ---- Print & mail provider (optional) ----------------------------------
MAIL_PROVIDER = env("MAIL_PROVIDER", "manual")            # "manual" or "lob"
LOB_API_KEY = env("LOB_API_KEY", "")                      # use a Lob TEST key first
HOA_RETURN_LINE1 = env("HOA_RETURN_LINE1", "")            # return address printed on mailed letters
HOA_RETURN_CITY = env("HOA_RETURN_CITY", "Sarasota")
HOA_RETURN_STATE = env("HOA_RETURN_STATE", "FL")
HOA_RETURN_ZIP = env("HOA_RETURN_ZIP", "")

# ---- Optional AI help finding the right violation ----------------------
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")   # leave blank to hide the "Ask AI" button
AI_MODEL = env("AI_MODEL", "claude-opus-5-5")      # only the typed description + the public rule titles are sent

# ---- Letterhead (shown on every notice; no management company) ---------
HOA_ADDRESS_LINE1 = env("HOA_ADDRESS_LINE1", env("HOA_RETURN_LINE1", ""))
HOA_ADDRESS_CITY = env("HOA_ADDRESS_CITY", env("HOA_RETURN_CITY", "Sarasota"))
HOA_ADDRESS_STATE = env("HOA_ADDRESS_STATE", env("HOA_RETURN_STATE", "FL"))
HOA_ADDRESS_ZIP = env("HOA_ADDRESS_ZIP", env("HOA_RETURN_ZIP", ""))
HOA_PHONE = env("HOA_PHONE", "")
HOA_WEBSITE = env("HOA_WEBSITE", "")
HOA_CONTACT_EMAIL = env("HOA_CONTACT_EMAIL", "")   # where owners send the response form

# ---- Cure-deadline reminder emails -------------------------------------
# Days before the deadline to email the owner (and tenant), e.g. "7,2" on a 15-day cure period.
REMINDER_DAYS = tuple(sorted({int(x) for x in env("REMINDER_DAYS", "7,2").split(",") if x.strip()}, reverse=True))
