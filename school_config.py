'''
School-specific settings (name, email senders, logo), read from the .env file.

Copy .env.example to .env and fill it in. Nothing school-specific is hard-coded in the scripts;
everything that identifies your school or its accounts lives in .env (which is git-ignored).

    from school_config import SCHOOL_SHORT_NAME, sender_for
    email, app_password = sender_for('HS')
'''

import os

from db import BASE_DIR, load_env

_ENV = load_env()


def _get(key, default=''):
    return os.environ.get(key) or _ENV.get(key) or default


SCHOOL_NAME = _get('SCHOOL_NAME', 'Your School')
SCHOOL_SHORT_NAME = _get('SCHOOL_SHORT_NAME', 'School')           # used in email subjects
EMAIL_SIGNATURE = _get('EMAIL_SIGNATURE', f'{SCHOOL_SHORT_NAME} IT')

# Header image on the HTML grade reports (optional; no logo if the file is missing).
_logo = _get('REPORT_LOGO_FILE', 'report_logo.png')
REPORT_LOGO_FILE = _logo if os.path.isabs(_logo) else os.path.join(BASE_DIR, _logo)

SMTP_SERVER = _get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(_get('SMTP_PORT', '587'))


def sender_for(school):
    """(sender email, Gmail app password) for 'HS' or 'JH', from .env."""
    email = _get(f'{school}_SENDER_EMAIL')
    password = _get(f'{school}_SENDER_APP_PASSWORD')
    if not email or not password:
        raise RuntimeError(f"Set {school}_SENDER_EMAIL and {school}_SENDER_APP_PASSWORD in .env "
                           f"(see .env.example) before sending email.")
    return email, password
