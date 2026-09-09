import logging
import os
from pathlib import Path

from django.utils.log import DEFAULT_LOGGING

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent

def _env_bool(name, default=False):
    """Parse a boolean from the environment tolerantly.

    Deliberately NOT production's bool(int(os.environ.get('DEBUG', 0))), which raises ValueError at
    import time on 'false'/'true'/'False' -- and because LOG_FILE_PATH keys off DEBUG, that failure
    also decides whether Django tries to open /var/log/indabom.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


# Deployment configuration from the environment. Placed BEFORE the local_settings import on
# purpose: local_settings.py remains the higher-precedence override for local development, while a
# container with no local_settings.py is configured entirely from env.
SECRET_KEY = os.environ.get('SECRET_KEY', 'insecure-development-key-change-me')
DEBUG = _env_bool('DEBUG', False)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if h.strip()]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DATABASE_NAME', BASE_DIR / 'db.sqlite3'),
    }
}

BOM_SOURCING_ENCRYPTION_KEYS = [
    k.strip() for k in os.environ.get('BOM_SOURCING_ENCRYPTION_KEYS', '').split(',') if k.strip()
]

# Email. Required by the part-class approval workflow, which notifies the next assignees on every
# state change (bom/functions.py). NEITHER upstream NOR the production fork defined a single
# EMAIL_* setting, so from_email resolved to '', Django substituted 'webmaster@localhost', the
# container ran no MTA, and send_mail's fail_silently=True swallowed the refused connection. The
# result was three years of notifications that were never sent and never logged. Configure the
# backend explicitly, and make the default a backend that CANNOT fail silently.
#
#   SENDGRID_API_KEY set  -> real mail via django-sendgrid-v5 (already a dependency)
#   EMAIL_BACKEND set     -> whatever the operator names
#   neither               -> console backend; mail is visible in the container log
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
if os.environ.get('EMAIL_BACKEND'):
    EMAIL_BACKEND = os.environ['EMAIL_BACKEND']
elif SENDGRID_API_KEY:
    EMAIL_BACKEND = 'sendgrid_backend.SendgridBackend'
    SENDGRID_SANDBOX_MODE_IN_DEBUG = _env_bool('SENDGRID_SANDBOX_MODE_IN_DEBUG', True)
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'indabom@simplyembedded.ca')
SERVER_EMAIL = os.environ.get('SERVER_EMAIL', DEFAULT_FROM_EMAIL)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '25'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = _env_bool('EMAIL_USE_TLS', False)

# Harmless on plain HTTP today; the day TLS is terminated in front of this, every POST would
# otherwise fail CSRF with an error that points nowhere near here.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

try:
    from .local_settings import *
except ImportError:
    logger.warning("local_settings.py not found. Using default settings.")
    pass

BOM_CONFIG = {}
BOM_CONFIG_DEFAULT = {
    'base_template': 'base.html',
    'standalone_mode': True,
    'admin_dashboard': {
        'enable_autocomplete': True,
        'page_size': 50,
    }
}
BOM_ORGANIZATION_MODEL = 'bom.Organization'
BOM_USER_META_MODEL = 'bom.UserMeta'

# Apply custom settings over defaults
bom_config_new = BOM_CONFIG_DEFAULT.copy()
bom_config_new.update(BOM_CONFIG)
BOM_CONFIG = bom_config_new


# --------------------------------------------------------------------------
# APPLICATION DEFINITION
# --------------------------------------------------------------------------

INSTALLED_APPS = [
    # Custom Apps first
    'bom.apps.BomConfig',

    # Django contrib apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party apps
    'materializecssform',
    'social_django',
    'djmoney',
    'djmoney.contrib.exchange',
]

try:
    import hijack

    INSTALLED_APPS.append('hijack')
    INSTALLED_APPS.append('hijack.contrib.admin')
except ImportError:
    pass

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'social_django.middleware.SocialAuthExceptionMiddleware',
]

if 'hijack' in INSTALLED_APPS:
    MIDDLEWARE.append('hijack.middleware.HijackUserMiddleware')

ROOT_URLCONF = 'bom.urls'
WSGI_APPLICATION = 'bom.wsgi.application'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# --------------------------------------------------------------------------
# TEMPLATES
# --------------------------------------------------------------------------

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # Use pathlib syntax for cleaner path joining
        'DIRS': [BASE_DIR / 'bom' / 'templates' / 'bom'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.media',
                'social_django.context_processors.backends',
                'social_django.context_processors.login_redirect',
                'bom.context_processors.bom_config',
            ],
        },
    },
]


# --------------------------------------------------------------------------
# AUTHENTICATION & SECURITY
# --------------------------------------------------------------------------

AUTHENTICATION_BACKENDS = (
    'social_core.backends.google.GoogleOAuth2',
    'bom.auth_backends.OrganizationPermissionBackend',
    'django.contrib.auth.backends.ModelBackend',
)

# Password validation - kept as is

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

# Social Auth Settings - kept as is
SOCIAL_AUTH_GOOGLE_OAUTH2_SCOPE = ['email', 'profile', 'https://www.googleapis.com/auth/drive.file', ]
SOCIAL_AUTH_GOOGLE_OAUTH2_AUTH_EXTRA_ARGUMENTS = {
    'access_type': 'offline',
    'approval_prompt': 'force'
}

SOCIAL_AUTH_PIPELINE = (
    'social_core.pipeline.social_auth.social_details',
    'social_core.pipeline.social_auth.social_uid',
    'social_core.pipeline.social_auth.social_user',
    'social_core.pipeline.user.get_username',
    'social_core.pipeline.social_auth.associate_by_email',
    'social_core.pipeline.user.create_user',
    'social_core.pipeline.social_auth.associate_user',
    'social_core.pipeline.social_auth.load_extra_data',
    'social_core.pipeline.user.user_details',
    'bom.third_party_apis.google_drive.store_drive_scope',
    'bom.third_party_apis.google_drive.initialize_parent',
)

SOCIAL_AUTH_DISCONNECT_PIPELINE = (
    'social_core.pipeline.disconnect.allowed_to_disconnect',
    'bom.third_party_apis.google_drive.uninitialize_parent',
    'social_core.pipeline.disconnect.get_entries',
    'social_core.pipeline.disconnect.revoke_tokens',
    'social_core.pipeline.disconnect.disconnect',
)


# --------------------------------------------------------------------------
# I18N & TIME
# --------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_L10N = True # Deprecated in Django 4.0, but harmless for now
USE_TZ = True


# --------------------------------------------------------------------------
# FILE STORAGE (Static and Media)
# --------------------------------------------------------------------------

# Env-overridable, upstream's pathlib defaults retained as the fallback. STATIC_ROOT matters
# disproportionately: if nginx serves a volume collectstatic never wrote to, every page renders
# completely unstyled with nothing in any log -- it reads as a CSS bug, not a config bug.
STATIC_URL = os.environ.get('STATIC_URL', '/static/')
STATIC_ROOT = os.environ.get('STATIC_ROOT', BASE_DIR / 'static')

MEDIA_URL = os.environ.get('MEDIA_URL', '/media/')
MEDIA_ROOT = os.environ.get('MEDIA_ROOT', BASE_DIR / 'media')

# Use the Django 4.2+ STORAGES setting
STORAGES = {
    # Default is FileSystemStorage, configured by STATIC_ROOT/MEDIA_ROOT
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


# --------------------------------------------------------------------------
# URLS & REDIRECTS
# --------------------------------------------------------------------------

LOGIN_URL = '/login/'
LOGOUT_URL = '/logout/'

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

SOCIAL_AUTH_LOGIN_REDIRECT_URL = '/settings?tab_anchor=organization'
SOCIAL_AUTH_DISCONNECT_REDIRECT_URL = '/settings?tab_anchor=organization'
SOCIAL_AUTH_LOGIN_ERROR_URL = '/'

# Custom login url for BOM_LOGIN (kept for compatibility)
BOM_LOGIN_URL = None

# Formset-driven pages (part classes, quantities of measure, large BOMs) can post
# more fields than Django's default limit of 1000. Raise it to accommodate them.
DATA_UPLOAD_MAX_NUMBER_FIELDS = locals().get('DATA_UPLOAD_MAX_NUMBER_FIELDS', 50000)


# --------------------------------------------------------------------------
# DJMONEY CONFIG
# --------------------------------------------------------------------------

CURRENCY_DECIMAL_PLACES = 4
EXCHANGE_BACKEND = 'djmoney.contrib.exchange.backends.FixerBackend'


# --------------------------------------------------------------------------
# LOGGING
# --------------------------------------------------------------------------

# Set DEBUG to False here if not defined in local_settings
DEBUG = locals().get('DEBUG', False)
# Env-overridable. Upstream hardcodes /var/log/indabom/django.log whenever DEBUG is falsy, so a
# DEBUG=False run outside the container dies at import with
#   ValueError: Unable to configure handler 'logfile'
# before any management command executes. The default below preserves upstream's behaviour
# exactly; the env var exists so a non-container run can point it somewhere writable.
LOG_FILE_PATH = os.environ.get(
    'LOG_FILE_PATH',
    '/var/log/indabom/django.log' if not DEBUG else BASE_DIR / 'bom.log',
)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'mail_admins': {
            'class': 'django.utils.log.AdminEmailHandler',
            'level': 'ERROR',
            'include_html': True,
        },
        'logfile': {
            'class': 'logging.handlers.WatchedFileHandler',
            'filename': LOG_FILE_PATH,
            'formatter': 'timestamp', # Define a simple formatter
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'timestamp',
        },
    },
    'formatters': {
        'timestamp': {
            'format': "[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s"
        },
    },
    'loggers': {
        # Catchall logger
        '': {
            'handlers': ['console', 'logfile'],
            'level': 'INFO',
        },
        # Django logging
        'django': {
            'handlers': ['logfile'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['mail_admins', 'logfile'],
            'level': 'ERROR',
            'propagate': False,
        },
        # django-bom app
        'bom': {
            'handlers': ['logfile', 'console'],
            'level': 'INFO',
            'propagate': False
        },
    },
}