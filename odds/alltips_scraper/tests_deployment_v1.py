"""Deployment-readiness contract tests for the sprint 1E-A1 configuration.

Why this module exists
----------------------
Sprint 1E-A1 makes the service deployable - ``Dockerfile``, ``.dockerignore``,
``fly.toml`` and ``DATABASE_URL`` support - without deploying it. All of that is
configuration: no request path imports it, so nothing else in the suite would
notice if a later edit reversed one of those decisions. An image that went back
to ``manage.py runserver``, a secret copied into ``fly.toml``, a
``release_command`` that started refreshing snapshots, a PostgreSQL connection
setting that is unsafe behind a pooler, or a settings change that stopped
honouring ``DATABASE_URL`` would each surface for the first time at deploy time,
against a live service and a real database.

So this module pins the deployment contract from two ends:

1. **Behaviour.** ``DATABASE_URL`` really does select PostgreSQL and really does
   leave the SQLite default alone when it is absent, and each transport setting
   really is read from its own environment value. Both are observed by importing
   the shipped settings module in a fresh interpreter with a fixed environment,
   never by matching the text of the file that declares them.
2. **The shipped configuration.** ``Dockerfile``, ``.dockerignore``,
   ``fly.toml`` and ``odds/.env.example`` are read from disk - ``fly.toml``
   through ``tomllib`` - so the assertions describe the configuration the
   platform would receive rather than a copy of its text.

Ground rules
------------
* **No network, no database, no Docker.** Nothing here builds an image, calls
  Fly, fetches a page or reads a database row: ``SimpleTestCase`` throughout, and
  the only subprocess is the local interpreter importing the local settings
  module.
* **No secret, ever.** The only credential-shaped values here are obvious
  non-credentials (``probe_user``, ``not-a-real-password``, ``db.invalid``).
* **No deployment, no identity.** ``fly.toml`` is a template: it carries
  placeholders, not an app name, a region or a machine size, and these tests
  require it to stay that way until an app is actually created.
* **``/api/health/`` is the liveness contract.** Sprint 1E-A1 adds no
  ``/healthz``: ``fly.toml`` checks the legacy endpoint, and that route is pinned
  here instead.

See ``docs/DEPLOYMENT.md`` for the procedure these tests guard, and
``docs/RUNBOOK.md`` section 7 for the commands that run them.
"""

import json
import os
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import Resolver404, resolve

APP_DIR = Path(__file__).resolve().parent
ODDS_DIR = APP_DIR.parent
REPO_ROOT = ODDS_DIR.parent

DOCKERFILE = REPO_ROOT / 'Dockerfile'
DOCKERIGNORE = REPO_ROOT / '.dockerignore'
FLY_CONFIG = REPO_ROOT / 'fly.toml'
ENV_EXAMPLE = ODDS_DIR / '.env.example'

# The liveness route shared by the container, fly.toml's health check and the
# runbook; the port the image publishes; the WSGI callable its CMD names.
HEALTH_PATH = '/api/health/'
SERVICE_PORT = 8080
GUNICORN_APP = 'odds.wsgi:application'

# The six transport settings. Every one is an explicit environment value with a
# safe default, and none of them may follow DEBUG.
SECURITY_SETTINGS = (
    'SECURE_SSL_REDIRECT',
    'SESSION_COOKIE_SECURE',
    'CSRF_COOKIE_SECURE',
    'SECURE_HSTS_SECONDS',
    'SECURE_HSTS_INCLUDE_SUBDOMAINS',
    'SECURE_HSTS_PRELOAD',
)

# What an unset environment must resolve to, so a local server keeps working over
# plain HTTP and DEBUG=False on its own cannot switch HTTPS on.
SAFE_LOCAL_SECURITY = {
    'SECURE_SSL_REDIRECT': False,
    'SESSION_COOKIE_SECURE': False,
    'CSRF_COOKIE_SECURE': False,
    'SECURE_HSTS_SECONDS': 0,
    'SECURE_HSTS_INCLUDE_SUBDOMAINS': False,
    'SECURE_HSTS_PRELOAD': False,
}

# Environment value -> resolved setting for a deployment-shaped environment. Two
# of them are False on purpose: an explicit False has to be honoured as well.
DEPLOYED_SECURITY = {
    'SECURE_SSL_REDIRECT': ('True', True),
    'SESSION_COOKIE_SECURE': ('True', True),
    'CSRF_COOKIE_SECURE': ('True', True),
    'SECURE_HSTS_SECONDS': ('3600', 3600),
    'SECURE_HSTS_INCLUDE_SUBDOMAINS': ('False', False),
    'SECURE_HSTS_PRELOAD': ('False', False),
}

# The transport policy the shipped fly.toml [env] block deploys.
FLY_TRANSPORT_POLICY = {
    'SECURE_SSL_REDIRECT': 'True',
    'SESSION_COOKIE_SECURE': 'True',
    'CSRF_COOKIE_SECURE': 'True',
    'SECURE_HSTS_SECONDS': '31536000',
    'SECURE_HSTS_INCLUDE_SUBDOMAINS': 'True',
    'SECURE_HSTS_PRELOAD': 'True',
}

LOCAL_ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]']
RELEASE_COMMAND = 'python manage.py migrate --noinput'
FLY_FORBIDDEN_TOKENS = ('refresh_tips', 'collectstatic', 'loaddata', 'runserver')
IMAGE_FORBIDDEN_TOKENS = (
    'runserver', 'migrate', 'refresh_tips', 'collectstatic',
    'SECRET_KEY', 'DATABASE_URL',
)
REQUIRED_DOCKERIGNORE_PATTERNS = (
    '.env', '**/.env', '*.sqlite3', '**/db.sqlite3',
    '.venv/', '**/.venv/', 'myvenv/', '**/myvenv/',
)
FORBIDDEN_DOCKERIGNORE_PATTERNS = ('*', 'odds', 'odds/', '/odds')

# Values that let the shipped settings module import without a developer's
# odds/.env. None of them is a credential.
PROBE_SECRET_KEY = 'django-insecure-deployment-tests-0123456789abcdefghijkl'
PROBE_EMAIL = 'deployment-tests@example.invalid'
PROBE_SCRAPE_URL = 'https://deployment-tests.invalid'
PROBE_DATABASE_URL = (
    'postgres://probe_user:not-a-real-password@db.invalid:5432/oddmate_probe'
)

# Runs in a fresh interpreter and prints the deployment-relevant settings as
# JSON. ``unset`` marks a database key the settings module never configured,
# which is how an option is shown not to reach a backend at all.
SETTINGS_PROBE = (
    "import json, odds.settings as shipped;"
    "database = shipped.DATABASES['default'];"
    "options = database.get('OPTIONS') or {};"
    "print(json.dumps({"
    "'debug': shipped.DEBUG,"
    "'allowed_hosts': shipped.ALLOWED_HOSTS,"
    "'wsgi_application': shipped.WSGI_APPLICATION,"
    "'proxy_header': list(shipped.SECURE_PROXY_SSL_HEADER),"
    "'security': {name: getattr(shipped, name) for name in %r},"
    "'database_keys': sorted(database),"
    "'database': {"
    "'engine': database.get('ENGINE'),"
    "'name': str(database.get('NAME', '')),"
    "'user': database.get('USER'),"
    "'host': database.get('HOST'),"
    "'port': database.get('PORT'),"
    "'conn_max_age': database.get('CONN_MAX_AGE', 'unset'),"
    "'conn_health_checks': database.get('CONN_HEALTH_CHECKS', 'unset'),"
    "'disable_server_side_cursors': "
    "database.get('DISABLE_SERVER_SIDE_CURSORS', 'unset'),"
    "'options': sorted(options),"
    "'prepare_threshold': options.get('prepare_threshold', 'unset'),"
    "},"
    "}))"
) % (SECURITY_SETTINGS,)


def nested_keys(mapping):
    """Yield every key of a parsed TOML document, nested tables included."""
    for key, value in mapping.items():
        yield key
        if isinstance(value, dict):
            yield from nested_keys(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from nested_keys(item)


def dockerfile_instructions(text):
    """Return a Dockerfile's instructions: comments dropped, continuations
    joined. Claims are then about what Docker reads, not about the prose that
    explains it."""
    instructions = []
    pending = ''
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.endswith('\\'):
            pending += line[:-1].strip() + ' '
            continue
        instructions.append((pending + line).strip())
        pending = ''
    if pending.strip():
        instructions.append(pending.strip())
    return instructions


def parse_env_file(text):
    """Return the KEY=value pairs of a dotenv-style file, comments ignored."""
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if separator:
            values[key.strip()] = value.strip()
    return values


def load_shipped_settings(**environment_overrides):
    """Import ``odds.settings`` in a subprocess with a fixed environment.

    Django's test runner forces ``DEBUG = False`` on the live settings *after*
    they load, so module-level behaviour has to be observed where it is computed:
    a fresh interpreter running the shipped module. Every name the probe depends
    on is pinned here, because an environment variable wins over ``odds/.env`` in
    python-decouple and an empty ``DATABASE_URL`` counts as set.

    ``environment_overrides`` maps an environment name to a value; ``None``
    removes the name instead. Removing one (``ALLOWED_HOSTS``, the transport
    settings) is how the shipped *default* is observed - and if a developer's
    ``odds/.env`` sets that name too, the probe sees the local override, which is
    what the assertion message points at.
    """
    environment = dict(os.environ)
    environment.update({
        'SECRET_KEY': PROBE_SECRET_KEY,
        'DEFAULT_FROM_EMAIL': PROBE_EMAIL,
        'SCRAPE_URL': PROBE_SCRAPE_URL,
    })
    for name, value in environment_overrides.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    completed = subprocess.run(
        [sys.executable, '-c', SETTINGS_PROBE],
        cwd=str(ODDS_DIR),
        env=environment,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f'importing odds.settings failed (exit {completed.returncode}): '
            f'{completed.stderr.strip()}'
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


class DatabaseConfigurationTests(SimpleTestCase):
    """DATABASE_URL decides the database; SQLite is only the local default."""

    def test_sqlite_default_is_used_and_carries_no_postgres_option(self):
        probe = load_shipped_settings(DATABASE_URL='')
        database = probe['database']
        self.assertEqual(
            database['engine'],
            'django.db.backends.sqlite3',
            'a checkout with no DATABASE_URL must run on the local SQLite file',
        )
        self.assertTrue(
            database['name'].replace('\\', '/').endswith('odds/db.sqlite3'),
            f'unexpected local database path: {database["name"]}',
        )
        # Engine and file name are the only keys configured, so no
        # PostgreSQL-only connection option can reach the SQLite default.
        self.assertEqual(probe['database_keys'], ['ENGINE', 'NAME'])
        self.assertEqual(database['options'], [])
        for option in ('conn_max_age', 'conn_health_checks',
                       'disable_server_side_cursors', 'prepare_threshold'):
            with self.subTest(option=option):
                self.assertEqual(database[option], 'unset')

    def test_database_url_selects_postgres_with_its_own_credentials(self):
        database = load_shipped_settings(DATABASE_URL=PROBE_DATABASE_URL)['database']
        self.assertEqual(
            database['engine'],
            'django.db.backends.postgresql',
            'a deployed service sets DATABASE_URL and must not fall back to SQLite',
        )
        for field, expected in (
            ('name', 'oddmate_probe'),
            ('user', 'probe_user'),
            ('host', 'db.invalid'),
            ('port', '5432'),
        ):
            with self.subTest(field=field):
                # str() because dj-database-url hands the port back as a number
                # in some versions and as a string in others.
                self.assertEqual(str(database[field]), expected)

    def test_postgres_connection_options_are_pooling_safe(self):
        database = load_shipped_settings(DATABASE_URL=PROBE_DATABASE_URL)['database']
        self.assertEqual(
            database['conn_max_age'],
            0,
            'a pooled deployment must not reuse connections it does not own',
        )
        self.assertIs(
            database['disable_server_side_cursors'],
            True,
            'a pooled deployment must not hold server-side cursors',
        )
        self.assertIn('prepare_threshold', database['options'])
        self.assertIsNone(
            database['prepare_threshold'],
            'automatic prepared statements are unsafe under transaction pooling',
        )
        self.assertIsNot(
            database['conn_health_checks'],
            True,
            'CONN_MAX_AGE=0 needs no health check: no connection outlives it',
        )


class SecuritySettingsTests(SimpleTestCase):
    """The six transport settings are explicit; DEBUG never drives them."""

    def test_local_defaults_are_safe_and_do_not_follow_debug(self):
        # The settings are removed from the child environment, so these are the
        # shipped defaults for a developer machine. DEBUG is flipped between the
        # two probes and must not change any of them.
        unset = dict.fromkeys(SECURITY_SETTINGS)
        debug_off = load_shipped_settings(DEBUG='False', **unset)['security']
        debug_on = load_shipped_settings(DEBUG='True', **unset)['security']
        for name in SECURITY_SETTINGS:
            with self.subTest(setting=name):
                self.assertEqual(
                    debug_off[name],
                    SAFE_LOCAL_SECURITY[name],
                    f'{name} must default to {SAFE_LOCAL_SECURITY[name]!r}; an '
                    'override in odds/.env would explain a different value',
                )
                self.assertEqual(
                    debug_on[name],
                    SAFE_LOCAL_SECURITY[name],
                    f'DEBUG=True must not change {name}',
                )

    def test_each_deployed_security_value_is_applied_on_its_own(self):
        # DEBUG=True as well: the explicit values have to win regardless of it.
        probe = load_shipped_settings(
            DEBUG='True',
            **{name: raw for name, (raw, _) in DEPLOYED_SECURITY.items()},
        )
        self.assertIs(probe['debug'], True)
        for name, (raw, expected) in DEPLOYED_SECURITY.items():
            with self.subTest(setting=name, environment=raw):
                self.assertEqual(probe['security'][name], expected)

    def test_proxy_header_is_trusted_only_behind_the_platform_proxy(self):
        expected = ('HTTP_X_FORWARDED_PROTO', 'https')
        self.assertEqual(settings.SECURE_PROXY_SSL_HEADER, expected)
        self.assertEqual(
            load_shipped_settings(DATABASE_URL='')['proxy_header'],
            list(expected),
        )

    def test_allowed_hosts_default_is_local_and_never_a_wildcard(self):
        hosts = load_shipped_settings(ALLOWED_HOSTS=None)['allowed_hosts']
        self.assertTrue(hosts, 'an empty ALLOWED_HOSTS would reject every request')
        self.assertNotIn('*', hosts)
        self.assertEqual(
            hosts,
            LOCAL_ALLOWED_HOSTS,
            'the shipped default is local-only, so a deployment has to name its '
            'host explicitly; if this fails, odds/.env sets ALLOWED_HOSTS',
        )


class LivenessRouteTests(SimpleTestCase):
    """The route fly.toml probes is the route the runbook curls locally."""

    def test_health_route_resolves_and_no_second_probe_was_added(self):
        self.assertEqual(resolve(HEALTH_PATH).url_name, 'health')
        with self.assertRaises(Resolver404):
            resolve('/healthz')


class ImageContractTests(SimpleTestCase):
    """The image serves the WSGI callable with gunicorn - and never migrates."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instructions = dockerfile_instructions(
            DOCKERFILE.read_text(encoding='utf-8')
        )
        cls.body = '\n'.join(cls.instructions)

    def test_the_image_serves_the_documented_wsgi_callable_on_the_port(self):
        for token in (
            'FROM python:3.12-slim',
            f'EXPOSE {SERVICE_PORT}',
            f'PORT={SERVICE_PORT}',
            'gunicorn',
            GUNICORN_APP,
            '${PORT}',
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.body)
        # gunicorn itself cannot be imported on Windows (POSIX-only fcntl), so
        # the callable its CMD names is imported here instead of through
        # `gunicorn --check-config`.
        module_name, _, attribute = GUNICORN_APP.partition(':')
        module = import_module(module_name)
        self.assertTrue(callable(getattr(module, attribute)))
        self.assertEqual(settings.WSGI_APPLICATION, f'{module_name}.application')

    def test_the_image_never_migrates_refreshes_or_bakes_a_secret(self):
        for token in IMAGE_FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, self.body)


class BuildContextTests(SimpleTestCase):
    """.dockerignore is a security control: local state must not be copied."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.lines = [
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]

    def test_build_context_excludes_nested_env_database_and_venv_paths(self):
        for pattern in REQUIRED_DOCKERIGNORE_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, self.lines)
        # Nothing may exclude the service source - or the whole context.
        for pattern in FORBIDDEN_DOCKERIGNORE_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, self.lines)


class FlyConfigurationTests(SimpleTestCase):
    """fly.toml is a template: no identity, no secret, no sizing policy."""

    PLACEHOLDER = r'^<[a-z0-9-]+>$'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = FLY_CONFIG.read_text(encoding='utf-8')
        cls.config = tomllib.loads(cls.text)

    def test_fly_template_has_no_identity_and_one_release_action(self):
        # The app name and the region are chosen when the app is actually
        # created, so the committed values are literal placeholders: naming a
        # real one here would be a guessed identity for a service that is not
        # deployed.
        self.assertRegex(self.config['app'], self.PLACEHOLDER)
        self.assertRegex(self.config['primary_region'], self.PLACEHOLDER)
        # No machine size either - no [[vm]], no size/memory key anywhere.
        self.assertNotIn('vm', self.config)
        self.assertNotIn('shared-cpu', self.text)
        for key in nested_keys(self.config):
            with self.subTest(key=key):
                self.assertNotIn(key, ('size', 'memory', 'cpus', 'cpu_kind'))
        # Exactly one release action, and it only migrates.
        self.assertEqual(self.config['deploy']['release_command'], RELEASE_COMMAND)
        for token in FLY_FORBIDDEN_TOKENS:
            with self.subTest(forbidden=token):
                self.assertNotIn(token, self.text)

    def test_fly_service_uses_the_health_route_and_keeps_a_machine_up(self):
        service = self.config['http_service']
        self.assertEqual(service['internal_port'], SERVICE_PORT)
        self.assertIs(service['force_https'], True)
        self.assertEqual(service['min_machines_running'], 1)
        self.assertEqual(service['auto_stop_machines'], 'off')
        checks = service['checks']
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['method'], 'GET')
        self.assertEqual(checks[0]['path'], HEALTH_PATH)

    def test_fly_env_declares_non_secrets_and_the_transport_policy(self):
        env = self.config['env']
        for name in ('SECRET_KEY', 'DATABASE_URL', 'DEBUG',
                     'CORS_ALLOW_ALL_ORIGINS'):
            with self.subTest(forbidden=name):
                self.assertNotIn(name, env)
        self.assertEqual(env['PORT'], str(SERVICE_PORT))
        self.assertTrue(env['DEFAULT_FROM_EMAIL'])
        self.assertTrue(env['SCRAPE_URL'].startswith('https://'))
        self.assertFalse(env['SCRAPE_URL'].endswith('/'))
        hosts = env['ALLOWED_HOSTS']
        self.assertNotIn('*', hosts)
        self.assertIn('<', hosts, 'the real <app>.fly.dev host is not chosen yet')
        for name, expected in FLY_TRANSPORT_POLICY.items():
            with self.subTest(setting=name):
                self.assertEqual(env[name], expected)


class EnvExampleTests(SimpleTestCase):
    """The tracked example documents every value and ships no real one."""

    PLACEHOLDERS = {
        'SECRET_KEY': 'replace-with-a-generated-secret-key',
        'DATABASE_URL': '',
        'DEBUG': 'False',
        'CORS_ALLOW_ALL_ORIGINS': 'False',
        'EMAIL_HOST_USER': '',
        'EMAIL_HOST_PASSWORD': '',
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = ENV_EXAMPLE.read_text(encoding='utf-8')
        cls.values = parse_env_file(cls.text)

    def test_env_example_holds_placeholders_and_safe_defaults_only(self):
        for name, expected in self.PLACEHOLDERS.items():
            with self.subTest(key=name):
                self.assertEqual(self.values[name], expected)
        # The transport settings are documented too, at their safe defaults.
        for name in SECURITY_SETTINGS:
            with self.subTest(key=name):
                self.assertEqual(self.values[name], str(SAFE_LOCAL_SECURITY[name]))
        # A tracked database URL is a shape to copy, never a credential.
        examples = [line for line in self.text.splitlines() if 'postgres://' in line]
        self.assertTrue(examples, 'the example must show the DATABASE_URL shape')
        for line in examples:
            with self.subTest(line=line):
                self.assertIn('PASSWORD', line)
