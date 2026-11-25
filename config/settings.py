"""Django settings for the Installment Ledger API."""
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


# Render sets RENDER=true and RENDER_EXTERNAL_HOSTNAME (e.g. my-app.onrender.com).
ON_RENDER = env_bool("RENDER")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
if ON_RENDER and SECRET_KEY == "dev-insecure-change-me":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is not set. Add a long random value in Render -> Environment.")
DEBUG = env_bool("DJANGO_DEBUG", not ON_RENDER)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
if RENDER_HOST:
    ALLOWED_HOSTS.append(RENDER_HOST)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "accounts",
    "ledger",
    "expenses",
    "finance",
    "assistant",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "config.wsgi.application"

def postgres_from_url(url):
    """postgresql://user:pass@host:port/db?sslmode=require&... -> Django DATABASES entry."""
    parts = urlparse(url)
    options = {k: v[-1] for k, v in parse_qs(parts.query).items()}  # sslmode, channel_binding, ...
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or 5432),
        "OPTIONS": options,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }


# DATABASE_URL (e.g. Neon) wins; otherwise the POSTGRES_* vars. USE_SQLITE=1 is only for quick local tests.
if env_bool("USE_SQLITE"):
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3", "OPTIONS": {"timeout": 20}}}
elif os.getenv("DATABASE_URL"):
    DATABASES = {"default": postgres_from_url(os.environ["DATABASE_URL"])}
elif ON_RENDER:
    # Without this the app would silently try localhost:5432 and every API call would 500.
    raise ImproperlyConfigured("DATABASE_URL is not set. Add the Neon connection string in Render -> Environment.")
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "installment_ledger"),
            "USER": os.getenv("POSTGRES_USER", "ledger"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", "ledger"),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Frontend origins allowed to call the API, e.g. "https://installment-ledger.vercel.app".
# A trailing slash or path would make django-cors-headers fail its checks (and the deploy), so strip it.
CORS_ALLOWED_ORIGINS = [
    o.rstrip("/") for o in env_list("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
]
# Regexes for changing hostnames, e.g. Vercel preview deployments: ^https://installment-ledger[a-z0-9-]*\.vercel\.app$
CORS_ALLOWED_ORIGIN_REGEXES = env_list("CORS_ALLOWED_ORIGIN_REGEXES")
CSRF_TRUSTED_ORIGINS = [f"https://{RENDER_HOST}"] if RENDER_HOST else []

if not DEBUG:
    # Render terminates HTTPS at its proxy and forwards X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Firebase: sign-in (only the project id is needed) and receipt storage (needs a service account; until one is
# configured, receipts stay in the database). See accounts/firebase_auth.py and ledger/receipt_storage.py.
# Gemini (cloud LLM). Server-side only; see agents.md §21.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
# Used when the main model is overloaded/unavailable (503 UNAVAILABLE).
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET", "")
FIREBASE_SERVICE_ACCOUNT_FILE = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "")
FIREBASE_SERVICE_ACCOUNT_B64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64", "")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "accounts.firebase_auth.FirebaseAuthentication",
        "accounts.security.LegacyJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    # Scoped limits only where calls cost money or hit third parties (views opt in with `throttle_scope`).
    "DEFAULT_THROTTLE_CLASSES": ("rest_framework.throttling.ScopedRateThrottle",),
    "DEFAULT_THROTTLE_RATES": {"ai_test": os.getenv("AI_TEST_RATE", "20/hour"), "ai_planner": os.getenv("AI_PLANNER_RATE", "30/hour"),
                               "ai_opportunities": os.getenv("AI_OPPORTUNITIES_RATE", "5/hour")},
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(os.getenv("JWT_ACCESS_MINUTES", "15"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.getenv("JWT_REFRESH_DAYS", "7"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}
