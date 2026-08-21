"""
Django settings for the Luxury Spa booking backend.

Configuration is read from environment variables (see .env.example).
A tiny .env loader is included so you don't need extra packages.
"""

import os
from pathlib import Path

# pyrefly: ignore [missing-import]
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=value lines, ignores blanks and #comments."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Core -------------------------------------------------------------------

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "bookings",
]

MIDDLEWARE = [
    "bookings.middleware.SimpleCorsMiddleware",  # must come first
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "spabackend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "spabackend.wsgi.application"

# --- Database ---------------------------------------------------------------
# SQLite file on your laptop. Swap ENGINE/NAME for Postgres later if needed.


if os.environ.get('DATABASE_URL'):
        DATABASES = {
        'default': dj_database_url.config(
            env='DATABASE_URL',
            conn_max_age=600,
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Singapore")
USE_I18N = True
USE_TZ = True

# Show times as 24-hour (14:30), matching the booking form's dropdown values,
# instead of Django's default US style (2:30 p.m.).
TIME_FORMAT = "H:i"
DATE_FORMAT = "Y-m-d"
DATETIME_FORMAT = "Y-m-d H:i"
USE_THOUSAND_SEPARATOR = False

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Booking agent settings -------------------------------------------------

# Public site the booking form lives on (used for the redirect after booking).
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://v26r5tzydt.konigle.net/")

# Cloudflare Turnstile. Leave TURNSTILE_SECRET blank and REQUIRE_CAPTCHA=false
# while testing locally.
REQUIRE_CAPTCHA = env_bool("REQUIRE_CAPTCHA", False)
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
TURNSTILE_SITEKEY = os.environ.get("TURNSTILE_SITEKEY", "")

# Optional shared secret for server-to-server calls to /api/book/.
# If blank, the public booking form still works without a key.
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")

# Origins allowed to POST to the API from a browser. "*" is fine locally.
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Needed so the live site can POST to your machine without CSRF rejection
CSRF_TRUSTED_ORIGINS = [o for o in CORS_ALLOWED_ORIGINS if o.startswith("http")]

# --- Email -------------------------------------------------------------------
# Leave EMAIL_HOST blank (the default) and bookings/services.py prints
# customer-facing emails to the console + saves a copy on the ContactMessage
# (visible in the admin) instead of sending anything real. Fill in these
# values from your SMTP provider (e.g. a transactional email service) once
# you're ready to send for real -- no code changes needed, services.py
# already checks EMAIL_HOST and switches over automatically.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587") or 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@luxuryspa.example")
