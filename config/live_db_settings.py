"""
Settings that talk to the LIVE database, whatever ``.env`` currently points at.

``.env`` gets flipped between the live box and the test one, and a command run
against the wrong target is silent — it reads as "row not found" or writes into
an environment nobody is looking at. This module removes the flipping: the
database comes from ``.env.live``, so aiming a command at production is
something you *say* rather than something the file happens to be set to::

    python manage.py seed_org_chart --settings=config.live_db_settings
    python manage.py shell         --settings=config.live_db_settings

Only ``DATABASES`` is taken from ``.env.live``; everything else is the ordinary
settings module, so SAP, OMS and the rest still follow ``.env``. Set up once
with a copy of ``.env`` taken while it pointed at production::

    cp .env .env.live      # gitignored — it holds a production credential

Writes through here land on production. Every one of them has to be deliberate.
"""

from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403
from .settings import BASE_DIR

LIVE_ENV_FILE = Path(BASE_DIR) / '.env.live'

if not LIVE_ENV_FILE.exists():
    raise ImproperlyConfigured(
        f"{LIVE_ENV_FILE} is missing. Copy .env to .env.live while .env points at "
        "the live database — it is gitignored and holds a production credential."
    )


def _read_env_file(path):
    """Parse a KEY=VALUE file, and *only* that file.

    Deliberately not ``decouple.Config``: it reads ``os.environ`` before the
    file, so a stray ``DB_HOST`` in the shell would quietly redirect a command
    that says "live" on the tin. Reading the file alone is the whole point here.
    """
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_live = _read_env_file(LIVE_ENV_FILE)

try:
    DATABASES = {
        'default': {
            'ENGINE': _live.get('DB_ENGINE', 'django.db.backends.postgresql'),
            'NAME': _live['DB_NAME'],
            'USER': _live['DB_USER'],
            'PASSWORD': _live['DB_PASSWORD'],
            'HOST': _live['DB_HOST'],
            'PORT': _live.get('DB_PORT', '5432'),
        }
    }
except KeyError as missing:
    raise ImproperlyConfigured(
        f"{LIVE_ENV_FILE} has no {missing.args[0]}. It needs the DB_* block from a "
        ".env aimed at production."
    ) from None

# The point of this module is the guarantee in its name, so it checks rather than
# trusts: a .env.live snapshotted while .env pointed somewhere else would other-
# wise carry the "live" label to the test box. Update these two if production
# genuinely moves.
LIVE_DB_HOST = '138.252.101.117'
LIVE_DB_NAME = 'factory_flow'

if (DATABASES['default']['HOST'], DATABASES['default']['NAME']) != (LIVE_DB_HOST, LIVE_DB_NAME):
    raise ImproperlyConfigured(
        f"{LIVE_ENV_FILE} points at {DATABASES['default']['HOST']}/"
        f"{DATABASES['default']['NAME']}, not the live {LIVE_DB_HOST}/{LIVE_DB_NAME}. "
        "Re-take the snapshot from a .env aimed at production, or update "
        "LIVE_DB_HOST / LIVE_DB_NAME in config/live_db_settings.py if it has moved."
    )
