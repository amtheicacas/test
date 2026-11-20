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
SESSION_COOKIE_AGE = int(env("SESSION_HOURS", "12")) * 3600   # staff; owners get 30 days (see signals.py)
PASSWORD_RESET_TIMEOUT = 86400
# (password reset links last one day: PASSWORD_RESET_TIMEOUT above)
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
FINE_PAYMENT_DAYS = int(env("FINE_PAYMENT_DAYS", "30"))       # fine due at least this many days after delivery of the decision letter...
FINE_MAIL_BUFFER_DAYS = int(env("FINE_MAIL_BUFFER_DAYS", "5"))  # ...plus these days for mail to arrive
HOA_DOCUMENTS_URL = env("HOA_DOCUMENTS_URL", "https://home.sunstatemanagement.com/login")   # where owners find the governing documents
ZOOM_ACCOUNT_ID = env("ZOOM_ACCOUNT_ID", "")       # Zoom Server-to-Server OAuth app: lets the app create the hearing's Zoom meeting
ZOOM_CLIENT_ID = env("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = env("ZOOM_CLIENT_SECRET", "")
ZOOM_HOST_EMAIL = env("ZOOM_HOST_EMAIL", "")       # the Zoom user who hosts (blank = the app's own user)
ZOOM_MEETING_MINUTES = int(env("ZOOM_MEETING_MINUTES", "120"))
ATTORNEY_NAME = env("ATTORNEY_NAME", "")      # the Association's attorney: pre-fills the "send to legal" email draft
ATTORNEY_EMAIL = env("ATTORNEY_EMAIL", "")
DECISION_LETTER_DAYS = int(env("DECISION_LETTER_DAYS", "7"))    # the decision letter must go out within this many days of the hearing
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
HOA_WEBSITE = env("HOA_WEBSITE", "www.myheronlanding.com")
HOA_CONTACT_EMAIL = env("HOA_CONTACT_EMAIL", "")   # where owners send the response form

# ---- Cure-deadline reminder emails -------------------------------------
# Days before the deadline to email the owner (and tenant), e.g. "7,2" on a 15-day cure period.
REMINDER_DAYS = tuple(sorted({int(x) for x in env("REMINDER_DAYS", "7,2").split(",") if x.strip()}, reverse=True))

# ---- Fining Resolution details ------------------------------------------
HEARING_NOTICE_RECOMMENDED_DAYS = int(env("HEARING_NOTICE_RECOMMENDED_DAYS", "20"))  # the Resolution recommends 20 (law: 14)
HEARING_REGULAR_COPY = env("HEARING_REGULAR_COPY", "1") == "1"   # optional extra first-class copy of the Notice of Hearing
HOA_CONTACT_NAME = env("HOA_CONTACT_NAME", "the Association")    # "contact ___ at phone or email" in the Notice of Hearing
HOA_PAYMENT_PAYABLE_TO = env("HOA_PAYMENT_PAYABLE_TO", "Heron Landing Homeowners Association, Inc.")
HOA_PAYMENT_ADDRESS = env("HOA_PAYMENT_ADDRESS", "")             # "c/o ..." where fines are mailed

MAX_LOGINS_PER_HOUSEHOLD = int(env("MAX_LOGINS_PER_HOUSEHOLD", "6"))
HOA_SIGNER_NAME = env("HOA_SIGNER_NAME", "")   # printed above "On Behalf of the Board of Directors" (e.g. the President)

# ---- Posting fines to the owner's ledger in Vantaca (optional; otherwise staff post by hand and record it) ----
VANTACA_LEDGER_POST_PATH = env("VANTACA_LEDGER_POST_PATH", "")   # the Vantaca endpoint that posts a charge to an owner account
VANTACA_FINE_CHARGE_CODE = env("VANTACA_FINE_CHARGE_CODE", "")   # the charge / GL code Vantaca uses for violation fines
# JSON body template. Placeholders: $account_id $amount $date $description $reference $charge_code
VANTACA_LEDGER_BODY = env("VANTACA_LEDGER_BODY", '{"accountId": "$account_id", "amount": $amount, "chargeCode": "$charge_code", '
                          '"date": "$date", "description": "$description", "reference": "$reference"}')
VANTACA_LEDGER_REF_KEY = env("VANTACA_LEDGER_REF_KEY", "id")     # key in Vantaca's reply holding the transaction id
VANTACA_LEDGER_ACCOUNT_FIELD = env("VANTACA_LEDGER_ACCOUNT_FIELD", "account_number")  # or "external_id" (Homeowner ID)

# ---- Map background tiles --------------------------------------------------
# OpenStreetMap's tile servers require the browser to say which site is asking (a Referer), and Django hides it by default.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
MAP_TILE_URL = env("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
MAP_TILE_ATTRIBUTION = env("MAP_TILE_ATTRIBUTION", "© OpenStreetMap contributors")
# Used automatically if the main tiles keep failing to load. For production use a provider with a key
# (MapTiler, Stadia Maps, Mapbox...) rather than OpenStreetMap's free servers, whose policy is for light use.
MAP_TILE_FALLBACK_URL = env("MAP_TILE_FALLBACK_URL", "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png")
MAP_TILE_FALLBACK_ATTRIBUTION = env("MAP_TILE_FALLBACK_ATTRIBUTION", "© OpenStreetMap contributors © CARTO")
# Satellite imagery for the map's Satellite view (Esri World Imagery; free for light use with attribution).
# For a production site use a provider with a key (MapTiler, Mapbox, Stadia...) and set these.
MAP_SATELLITE_URL = env("MAP_SATELLITE_URL", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}")
MAP_SATELLITE_ATTRIBUTION = env("MAP_SATELLITE_ATTRIBUTION", "Tiles © Esri, Maxar, Earthstar Geographics and the GIS User Community")

# Declaration 8.1(A)(i): a suspension of recreational-facility use may not exceed 60 days for an infraction (and may be renewed for successive
# 60-day periods while the infraction is uncorrected). Fines of $1,000 or more may be secured by a lien; smaller fines may not.
# First notices go out by email at once; the printed/mailed copy waits this many business days (Mon-Fri) in case the owner fixes it (saves postage).
BOARD_REVIEW_TARGET_DAYS = int(env("BOARD_REVIEW_TARGET_DAYS", "14"))   # the dashboard flags a case "overdue" when the Board has not voted this many days after the cure period ended
MAIL_DELAY_BUSINESS_DAYS = int(env("MAIL_DELAY_BUSINESS_DAYS", "2"))
X_FRAME_OPTIONS = "SAMEORIGIN"   # lets the app show its own letter PDFs inside its pages
STAFF_ADMIN_ACCESS = env("STAFF_ADMIN_ACCESS", "1") == "1"   # managers and board members get the same access, including the admin screens
SUSPENSION_MAX_DAYS = int(env("SUSPENSION_MAX_DAYS", "60"))
FINE_LIEN_THRESHOLD = int(env("FINE_LIEN_THRESHOLD", "1000"))
