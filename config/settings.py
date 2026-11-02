"""
Django settings for the Telegram Real-Time Message Monitoring System.

Local-development focused. No Celery/Redis — the Telegram listener and the
trading engine run as in-process background threads (see
telegram_monitor.apps and trading.apps); Channels' in-memory layer pushes
signals to the dashboard.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-change-me")
DEBUG = os.getenv("DEBUG", "True") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
INSTALLED_APPS = [
    # "daphne" first: makes `manage.py runserver` an ASGI server (HTTP + WS)
    # so the single `runserver` command still serves everything — no
    # separate WebSocket process. See config/asgi.py for the routing.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "channels",
    "runtime_settings",
    "telegram_monitor",
    "trading.apps.TradingConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------
# Database — PostgreSQL only
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DATABASE_NAME", "telegram_binance_automation"),
        "USER": os.getenv("DATABASE_USER", "postgres"),
        "PASSWORD": os.getenv("DATABASE_PASSWORD", ""),
        "HOST": os.getenv("DATABASE_HOST", "127.0.0.1"),
        "PORT": os.getenv("DATABASE_PORT", "5432"),
    }
}

# --------------------------------------------------------------------------
# Auth / password validation
# --------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# --------------------------------------------------------------------------
# i18n
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# --------------------------------------------------------------------------
# Runtime settings (the Settings page)
# --------------------------------------------------------------------------
# Everything that used to be tuned in .env (signal leverage / margin, trading switches,
# poll interval, fee, timeouts, parser keywords, ...) now lives in the database and is
# edited on the Settings page: see runtime_settings/registry.py for the full list.
# The old .env values are imported once by `migrate` (runtime_settings migration 0002),
# after which they can be deleted. Only secrets and deployment values stay here.
RUNTIME_SETTINGS_SEED_FROM_ENV = "test" not in sys.argv  # tests start from the defaults, not from a developer's .env
RUNTIME_SETTINGS_CACHE_SECONDS = 0 if "test" in sys.argv else 2.0

# --------------------------------------------------------------------------
# Telegram / Telethon credentials (secrets stay in .env)
# --------------------------------------------------------------------------
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "telegram_listener")
TELEGRAM_SESSIONS_DIR = BASE_DIR / "sessions"

# --------------------------------------------------------------------------
# Realtime (Django Channels)
# --------------------------------------------------------------------------
# In-memory layer: fine for local dev (single process). A multi-process /
# production deployment must switch to channels_redis.core.RedisChannelLayer
# (or another shared backend) since separate processes don't share memory.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

# --------------------------------------------------------------------------
# Binance credentials (secrets stay in .env)
# --------------------------------------------------------------------------
# Everything else about signals and trading (leverage, margin, testnet, switches, poll
# interval, fee, ...) is on the Settings page: see "Runtime settings" above.
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} - {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "telegram_monitor": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "telethon": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# --------------------------------------------------------------------------
# Quiet logging while running `manage.py test`
# --------------------------------------------------------------------------
# Several tests deliberately trigger failure paths (bad .env value, 403/405/400
# responses, a Telegram flood-wait, a parser crash ...) and the app logs those
# as WARNING/ERROR on purpose. That is correct in production but looks like real
# problems in the test output, so drop the console output during tests.
# (assertLogs still works: it installs its own handler.)
if "test" in sys.argv[1:2]:
    LOGGING["handlers"]["console"] = {"class": "logging.NullHandler"}
