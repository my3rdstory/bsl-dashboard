import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-change-me")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() == "true"
DEBUG_FAKE_LIGHTNING_LOGIN = (
    os.getenv("DEBUG_FAKE_LIGHTNING_LOGIN", "True").lower() == "true"
)
DEBUG_FAKE_LIGHTNING_FIXED_WALLET_KEY = os.getenv(
    "DEBUG_FAKE_LIGHTNING_FIXED_WALLET_KEY", ""
).strip()
LNURL_AUTH_ENABLED = os.getenv("LNURL_AUTH_ENABLED", "True").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.nav_profile",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES: dict[str, dict[str, object]] = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "bsl_dashboard"),
        "USER": os.getenv("POSTGRES_USER", "bsl_user"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "bsl_password"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

if os.getenv("DATABASE_URL"):
    DATABASES["default"] = dict(
        dj_database_url.parse(
            os.environ["DATABASE_URL"],
            conn_max_age=600,
            ssl_require=True,
        )
    )


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = "ko-kr"

TIME_ZONE = "Asia/Seoul"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGIN_REDIRECT_URL = "/me/"
LOGOUT_REDIRECT_URL = "/"

DEFAULT_MONTHLY_DUES_AMOUNT = int(os.getenv("DEFAULT_MONTHLY_DUES_AMOUNT", "100000"))

MEMPOOL_API_BASE_URL = os.getenv("MEMPOOL_API_BASE_URL", "").strip()
if not MEMPOOL_API_BASE_URL:
    MEMPOOL_API_BASE_URL = "https://mempool.space/testnet4" if DEBUG else "https://mempool.space"

BLINK_GRAPHQL_ENDPOINT = os.getenv("BLINK_GRAPHQL_ENDPOINT", "https://api.blink.sv/graphql").strip()
if not BLINK_GRAPHQL_ENDPOINT:
    BLINK_GRAPHQL_ENDPOINT = "https://api.blink.sv/graphql"

IWINV_OBJECT_STORAGE_ENDPOINT = os.getenv(
    "IWINV_OBJECT_STORAGE_ENDPOINT", "https://kr.object.iwinv.kr"
).strip()
IWINV_OBJECT_STORAGE_BUCKET = os.getenv(
    "IWINV_OBJECT_STORAGE_BUCKET", "bsl-dashboard"
).strip()
IWINV_OBJECT_STORAGE_ACCESS_KEY = os.getenv("IWINV_OBJECT_STORAGE_ACCESS_KEY", "").strip()
IWINV_OBJECT_STORAGE_SECRET_KEY = os.getenv("IWINV_OBJECT_STORAGE_SECRET_KEY", "").strip()
IWINV_OBJECT_STORAGE_SIGNATURE_VERSION = os.getenv(
    "IWINV_OBJECT_STORAGE_SIGNATURE_VERSION",
    "s3",
).strip()
IWINV_OBJECT_STORAGE_DOWNLOAD_EXPIRES = int(
    os.getenv("IWINV_OBJECT_STORAGE_DOWNLOAD_EXPIRES", "600")
)

DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@bsl.local").strip()
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend",
).strip()
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com").strip()
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").strip()
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
MESSAGE_SHARE_DISCORD_BOT_TOKEN = os.getenv(
    "MESSAGE_SHARE_DISCORD_BOT_TOKEN", ""
).strip()

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
