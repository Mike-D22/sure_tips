
from pathlib import Path

from decouple import Csv, config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')

# SECURITY WARNING: don't run with debug turned on in production!
# Defaults to False, so a missing or mistyped environment variable can never
# enable debug mode in a deployed environment. Local development opts in with
# DEBUG=True in odds/.env.
DEBUG = config('DEBUG', default=False, cast=bool)

# Comma-separated list of hosts. Defaults to local development hosts only;
# a wildcard is deliberately not the default.
ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1,[::1]',
    cast=Csv(),
)


# Application definition

INSTALLED_APPS = [
    'corsheaders',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'alltips_scraper',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# CORS ----------------------------------------------------------------------
# Wildcard CORS is opt-in and local-development only (CORS_ALLOW_ALL_ORIGINS=True
# in a local odds/.env). It is never the default configuration, because it lets
# any browser origin read the API.
CORS_ALLOW_ALL_ORIGINS = config('CORS_ALLOW_ALL_ORIGINS', default=False, cast=bool)

# Explicit origins are only consulted while the wildcard is off.
CORS_ALLOWED_ORIGINS = [] if CORS_ALLOW_ALL_ORIGINS else config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:8000,http://127.0.0.1:8000',
    cast=Csv(),
)

# Local development ports only; these never match a deployed origin.
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^http://localhost:\d+$",    # Any localhost port
    r"^http://127\.0\.0\.1:\d+$", # Any 127.0.0.1 port
]

ROOT_URLCONF = 'odds.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'odds.wsgi.application'


# Email -----------------------------------------------------------------------

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = True
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL')

# Database --------------------------------------------------------------------
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
#
# SQLite, and only SQLite. Local development, the test suite and the container
# all use the git-ignored SQLite file in this directory, so a fresh checkout runs
# with no database server and the same image runs anywhere.
#
# There is no DATABASE_URL path and no PostgreSQL support: DATABASE_URL is not
# read at all, so setting it - empty, absent or hostile - changes nothing here
# and cannot reach the database mapping. The connection options the old pooled
# PostgreSQL path needed (CONN_MAX_AGE, DISABLE_SERVER_SIDE_CURSORS,
# OPTIONS['prepare_threshold']) are gone with it.
#
# Django's own installed apps (admin, auth, sessions, contenttypes) and the
# versioned snapshot table still use this local file, which is why fly.toml's
# release_command still runs `manage.py migrate`.

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'


# Deployment security ---------------------------------------------------------
# Six explicit, independent environment values, each with a safe local default.
# Nothing here is derived from DEBUG, so DEBUG=False can never silently switch
# HTTPS redirect, secure-only cookies or HSTS on, and a deployment that forgets
# to set them serves a plain-HTTP-safe configuration rather than a half-applied
# one. A deployment sets all six in fly.toml's [env] block - they describe the
# transport policy and carry no credential, so they are not secrets; local
# development leaves them alone. docs/RUNBOOK.md 7 and 11 explain how the
# resulting configuration is checked and what a deployment sets.

SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=False, cast=bool)
SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=False, cast=bool)
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=False, cast=bool)

# Seconds of HSTS; 0 sends no Strict-Transport-Security header at all. The
# `preload` directive is only a header value - a domain has to be submitted to
# the browser preload list separately.
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=0, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config(
    'SECURE_HSTS_INCLUDE_SUBDOMAINS', default=False, cast=bool
)
SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=False, cast=bool)

# Fly terminates TLS at its edge and forwards the original scheme in
# X-Forwarded-Proto. Django has to trust that header before it can recognise an
# https request, because SECURE_SSL_REDIRECT without it would redirect a request
# that already arrived over TLS, forever. Trusting it is only sound while this
# process runs behind the platform proxy (the container is never exposed
# directly), so it is deliberately not something a request from anywhere else
# can claim.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# Logging ---------------------------------------------------------------------
# Console only. Scraper modules log via logging.getLogger(__name__); never log
# SECRET_KEY, SMTP credentials, or any other environment value.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': config('DJANGO_LOG_LEVEL', default='INFO'),
    },
    'loggers': {
        'alltips_scraper': {
            'handlers': ['console'],
            'level': config('ALLTIPS_LOG_LEVEL', default='INFO'),
            'propagate': False,
        },
    },
}
