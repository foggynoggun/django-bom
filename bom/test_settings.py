"""Settings for running the test suite.

Usage:  python manage.py test bom --settings=bom.test_settings

`bom.settings` still works and is what the plan's verification gate records against; this module
only removes two costs that have nothing to do with what the tests assert.

Every TestCase in bom/tests.py logs a client in during setUp, and Django 6's default PBKDF2
hasher runs ~1.4M iterations per call. That is ~20 minutes of the suite's runtime spent proving
that PBKDF2 works. MD5 here is not a security position -- it is scoped to the test database and
never reaches a deployed settings module.
"""

from .settings import *  # noqa: F401,F403

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Workflow notifications are asserted against mail.outbox rather than a live backend.
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
DEFAULT_FROM_EMAIL = 'test@example.com'
