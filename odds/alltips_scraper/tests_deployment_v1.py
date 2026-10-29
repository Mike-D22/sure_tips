"""Deployment-readiness contract tests for the sprint 1E-A1 configuration.

Why this module exists
----------------------
Sprint 1E-A1 makes the service deployable - ``Dockerfile``, ``.dockerignore`` and
``fly.toml`` - without deploying it. All of that is configuration: no request
path imports it, so nothing else in the suite would notice if a later edit
reversed one of those decisions. An image that went back to
``manage.py runserver``, a secret copied into ``fly.toml``, a ``release_command``
that started refreshing snapshots, or a settings change that let an environment
variable select the database would each surface for the first time at deploy
time, against a live service and a real database.

So this module pins the deployment contract from two ends:

1. **Behaviour.** The database is the shipped SQLite file and no environment
   value can select another one - a PostgreSQL-shaped ``DATABASE_URL`` is set and
   the shipped file still wins - and each transport setting really is read from
   its own environment value. Both are observed by importing the shipped
   settings module in a fresh interpreter with a fixed environment, never by
   matching the text of the file that declares them.
2. **The shipped configuration.** ``Dockerfile``, ``.dockerignore``,
   ``fly.toml`` and ``odds/.env.example`` are read from disk - ``fly.toml``
   through ``tomllib`` - so the assertions describe the configuration the
   platform would receive rather than a copy of its text. The two tracked CI
   workflows and the four documents that state the Fly identity are read from
   disk the same way, so the identity a reader is told cannot drift from the one
   ``fly.toml`` commits.

Ground rules
------------
* **No network, no database, no Docker.** Nothing here builds an image, calls
  Fly, fetches a page or reads a database row: ``SimpleTestCase`` throughout, and
  the only subprocess is the local interpreter importing the local settings
  module.
* **No secret, ever.** The only credential-shaped values here are obvious
  non-credentials (``probe_user``, ``not-a-real-password``, ``db.invalid``).
* **An identity is selected; no resource and no deployment exist.** Sprint
  1O-1P replaced the placeholder identity with one committed name, region and
  host, and these tests pin those exact values together with the documents that
  state them. None of that is a claim about Fly: no app has been created, no
  secret is set, nothing has been deployed and no production check has run, so
  no test here may assert an app, a release, a secret or a live response.
* **``/api/health/`` is the liveness contract.** Sprint 1E-A1 adds no
  ``/healthz``: ``fly.toml`` checks the legacy endpoint, and that route is pinned
  here instead.
* **The published content ships in the image.** The versioned endpoint is served
  from reviewed JSON inside the application package, so the content directory is
  part of the image contract: it may not be excluded from the build context, and
  the instruction that copies the service has to carry it in.

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

from .jsoncontent_v1 import CANONICAL_CONTENT_ROOT

APP_DIR = Path(__file__).resolve().parent
ODDS_DIR = APP_DIR.parent
REPO_ROOT = ODDS_DIR.parent

DOCKERFILE = REPO_ROOT / 'Dockerfile'
DOCKERIGNORE = REPO_ROOT / '.dockerignore'
FLY_CONFIG = REPO_ROOT / 'fly.toml'
ENV_EXAMPLE = ODDS_DIR / '.env.example'

# The tracked documents that state the Fly identity, plus the workflow directory:
# read from disk the same way as the shipped files above, so the identity a reader
# is told cannot drift from the one ``fly.toml`` commits.
README = REPO_ROOT / 'README.md'
DEPLOYMENT_DOC = REPO_ROOT / 'docs' / 'DEPLOYMENT.md'
RUNBOOK_DOC = REPO_ROOT / 'docs' / 'RUNBOOK.md'
AUDIT_DOC = REPO_ROOT / 'docs' / 'AUDIT.md'
FLY_IDENTITY_FILES = (
    FLY_CONFIG, README, DEPLOYMENT_DOC, RUNBOOK_DOC, AUDIT_DOC,
)
WORKFLOW_DIR = REPO_ROOT / '.github' / 'workflows'

# The canonical content the versioned endpoint is served from, and the one
# instruction that copies the service directory holding it into the image. The
# two are asserted against each other: the content lives under the directory the
# copy names, so the copy is what carries it.
CONTENT_ROOT = APP_DIR / 'content' / 'v1'
SHIPPED_MANIFEST = CONTENT_ROOT / 'manifest.json'
CONTENT_COPY_INSTRUCTION = 'COPY --chown=appuser:appuser odds/ ./odds/'

# The directory that copy lands in is created by the requirements copy before it,
# so it stays root-owned until this instruction hands it to the runtime account:
# only then can the release command create its ephemeral SQLite file there.
SERVICE_DIR_OWNER_INSTRUCTION = 'RUN chown appuser:appuser /app/odds'

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

# The Fly identity selected in sprint 1O-1P, and the spellings it removed. These
# pin what the tracked files say - never that a Fly resource exists: no
# ``fly apps create``, no secret, no deploy and no production observation has
# happened, and no test may assert one.
FLY_APP = 'sure-tips-api-mate'
FLY_REGION = 'iad'
FLY_HOST = FLY_APP + '.fly.dev'
FLY_ALLOWED_HOSTS = ','.join([FLY_HOST] + LOCAL_ALLOWED_HOSTS)
FLY_CREATE_COMMAND = 'fly apps create ' + FLY_APP

# The identity spellings this deployment used while its identity was still
# unresolved. A current-state file that still carries one is still describing a
# decided identity as an open question. ``<previous-version>`` is deliberately
# not here: it is a genuine metavariable of the documented rollback command.
RETIRED_FLY_IDENTITY_PLACEHOLDERS = (
    '<staging-or-production-app-name>',
    '<fly-region>',
    '<app>',
    '<app-name>',
)

# Each identity file has to keep saying, in its own words, that nothing exists
# yet. Any single phrase is enough: the files differ in wording, not in fact.
NO_FLY_RESOURCE_STATEMENTS = (
    'has not been created',
    'has never been run',
    'no fly app',
    'nothing has been deployed',
)

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
# A negative probe: credential-shaped like a real managed-PostgreSQL URL, so a
# test can set it and show that the shipped database mapping ignores it entirely.
# Nothing parses it, and ``db.invalid`` resolves nowhere.
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
    python-decouple and an empty value counts as one that is set.

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
    """The database is the shipped SQLite file; nothing selects another one."""

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

    def test_database_url_selects_nothing_and_no_credential_reaches_the_mapping(self):
        # The probe URL is credential-shaped on purpose: a regression that went
        # back to parsing DATABASE_URL would have to put its host, user and port
        # into the mapping these assertions read.
        probe = load_shipped_settings(DATABASE_URL=PROBE_DATABASE_URL)
        database = probe['database']
        self.assertEqual(
            database['engine'],
            'django.db.backends.sqlite3',
            'the database must not follow DATABASE_URL',
        )
        self.assertTrue(
            database['name'].replace('\\', '/').endswith('odds/db.sqlite3'),
            f'unexpected database path: {database["name"]}',
        )
        for field in ('user', 'host', 'port'):
            with self.subTest(field=field):
                self.assertIsNone(
                    database[field],
                    'no credential from DATABASE_URL may reach the mapping',
                )
        # Engine and file name are still the only keys configured even with a
        # PostgreSQL-shaped URL set, so nothing parsed it into a new key.
        self.assertEqual(probe['database_keys'], ['ENGINE', 'NAME'])

    def test_postgres_pooling_options_are_gone(self):
        # CONN_MAX_AGE, DISABLE_SERVER_SIDE_CURSORS and prepare_threshold existed
        # only to make a pooled PostgreSQL deployment safe. With that deployment
        # removed they must not be configured for any input - including a
        # PostgreSQL-shaped one, which is what this probe sets.
        database = load_shipped_settings(DATABASE_URL=PROBE_DATABASE_URL)['database']
        self.assertEqual(database['options'], [])
        for option in ('conn_max_age', 'conn_health_checks',
                       'disable_server_side_cursors', 'prepare_threshold'):
            with self.subTest(option=option):
                self.assertEqual(database[option], 'unset')


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
            load_shipped_settings()['proxy_header'],
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

    def test_the_service_directory_is_owned_by_the_runtime_account(self):
        # Only the entries a copy --chown names become appuser-owned; the directory
        # the copy lands in pre-exists it, so without this instruction the release
        # command cannot create /app/odds/db.sqlite3 and exits 1.
        self.assertIn(SERVICE_DIR_OWNER_INSTRUCTION, self.instructions)


class ShippedContentTests(SimpleTestCase):
    """The image carries the published content the deployed endpoint reads.

    ``/api/v1/tips/`` is answered from reviewed JSON inside the application
    package, so the content directory has to survive both halves of the build: it
    may not be excluded from the build context, and the instruction that copies
    the service has to carry it into the image. Nothing here builds an image - the
    Dockerfile and ``.dockerignore`` are read as the configuration Docker would
    receive.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instructions = dockerfile_instructions(
            DOCKERFILE.read_text(encoding='utf-8')
        )

    def test_the_content_root_is_the_directory_the_reader_defaults_to(self):
        # The reader's default root is the package directory, and this test's
        # path is built from the same place, so the two cannot drift apart.
        self.assertEqual(CONTENT_ROOT, CANONICAL_CONTENT_ROOT)
        self.assertTrue(CONTENT_ROOT.is_dir())

    def test_the_manifest_the_image_ships_is_the_pinned_empty_one(self):
        self.assertEqual(
            json.loads(SHIPPED_MANIFEST.read_text(encoding='utf-8')),
            {'schema_version': 1, 'snapshots': {}},
        )

    def test_the_image_copies_the_service_directory_that_holds_the_content(self):
        self.assertTrue(
            CONTENT_ROOT.is_relative_to(REPO_ROOT / 'odds'),
            'the content directory has to live under the copied directory',
        )
        self.assertIn(CONTENT_COPY_INSTRUCTION, self.instructions)

    def test_the_build_context_excludes_no_part_of_the_content_directory(self):
        lines = [
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]

        for pattern in lines:
            with self.subTest(pattern=pattern):
                self.assertNotIn('content', pattern)
                self.assertNotIn('alltips_scraper', pattern)
                self.assertNotIn('.json', pattern)


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
    """fly.toml carries the selected identity: no secret, no sizing policy."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = FLY_CONFIG.read_text(encoding='utf-8')
        cls.config = tomllib.loads(cls.text)

    def test_fly_identity_is_selected_and_one_release_action_remains(self):
        # The identity is chosen and committed (sprint 1O-1P), so the values are
        # pinned exactly rather than checked for placeholder shape: an app name
        # is not a free variable, and the host has to be this app's own host.
        # Nothing here claims a Fly resource exists - see
        # FlyIdentityDocumentationTests for that half of the contract.
        self.assertEqual(self.config['app'], FLY_APP)
        self.assertEqual(self.config['primary_region'], FLY_REGION)
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
        self.assertEqual(hosts, FLY_ALLOWED_HOSTS)
        self.assertEqual(
            hosts.split(','), [FLY_HOST] + LOCAL_ALLOWED_HOSTS,
            'this app\'s own host comes first and every local host stays, after it',
        )
        self.assertEqual(
            FLY_HOST, self.config['app'] + '.fly.dev',
            'the app name and its own Fly host are one decision, not two',
        )
        for name, expected in FLY_TRANSPORT_POLICY.items():
            with self.subTest(setting=name):
                self.assertEqual(env[name], expected)


class EnvExampleTests(SimpleTestCase):
    """The tracked example documents every value and ships no real one."""

    PLACEHOLDERS = {
        'SECRET_KEY': 'replace-with-a-generated-secret-key',
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
        # No database URL of any shape: the shipped settings read no
        # DATABASE_URL, so the example must not advertise one. The name may still
        # appear in the Database comment, but never as a KEY=value pair.
        self.assertNotIn('DATABASE_URL', self.values)
        self.assertNotIn('postgres://', self.text)


class FlyIdentityDocumentationTests(SimpleTestCase):
    """The selected identity is stated the same way in every tracked file.

    Sprint 1O-1P replaced the placeholder identity with one committed name,
    region and host, so the documents that describe the deployment have to match
    ``fly.toml`` instead of merely agreeing that nothing is chosen yet. What none
    of them may claim is a resource: the app is still uncreated, undeployed and
    unverified, and every identity file has to keep saying so.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.texts = {
            path: path.read_text(encoding='utf-8')
            for path in FLY_IDENTITY_FILES
        }

    def test_the_scan_covers_the_config_and_the_four_documents(self):
        # The scan scope is part of the contract: shrinking it would silently
        # stop pinning a document, so the tuple itself is pinned.
        self.assertEqual(
            FLY_IDENTITY_FILES,
            (FLY_CONFIG, README, DEPLOYMENT_DOC, RUNBOOK_DOC, AUDIT_DOC),
        )

    def test_every_identity_file_states_the_same_selected_identity(self):
        for path, text in self.texts.items():
            with self.subTest(file=path.name):
                self.assertIn(FLY_APP, text)
                self.assertIn(FLY_REGION, text)
                self.assertIn(FLY_HOST, text)

    def test_the_documented_create_command_names_the_committed_app(self):
        # An operator following the procedure has to create this app: a generic
        # ``fly apps create <something>`` would invite a second identity.
        for path in (DEPLOYMENT_DOC, RUNBOOK_DOC):
            with self.subTest(file=path.name):
                self.assertIn(FLY_CREATE_COMMAND, self.texts[path])

    def test_no_retired_identity_placeholder_survives(self):
        for path, text in self.texts.items():
            for retired in RETIRED_FLY_IDENTITY_PLACEHOLDERS:
                with self.subTest(file=path.name, retired=retired):
                    self.assertNotIn(retired, text)

    def test_every_identity_file_still_says_no_fly_resource_exists(self):
        for path, text in self.texts.items():
            with self.subTest(file=path.name):
                self.assertTrue(
                    any(
                        phrase in text.lower()
                        for phrase in NO_FLY_RESOURCE_STATEMENTS
                    ),
                    f'{path.name} must still state that the app has not been '
                    'created and nothing has been deployed',
                )


class WorkflowCapabilityTests(SimpleTestCase):
    """The two CI workflows stay test-only: no deploy, no token, no write.

    Sprint 1N made "green gates deploy nothing" a recorded rule. The rule is
    only worth recording if a later edit cannot quietly grant a workflow the
    capability to break it, so both files are read from disk and scanned for the
    syntax that would: a Fly token or the CLI, a registry push, an id token, an
    inherited or referenced secret, a write scope, or a trigger that hands a
    workflow base-repository context.
    """

    EXPECTED_WORKFLOWS = ('django-tests.yml', 'image-build-smoke.yml')

    # Capability *syntax*, not prose: an entry is spelled the way it would
    # appear as a key or a value, so a comment that names a capability in order
    # to forbid it does not fail this test.
    FORBIDDEN_CAPABILITY_SYNTAX = (
        'FLY_API_TOKEN',
        'flyctl',
        'fly deploy',
        'fly apps create',
        'registry.fly.io',
        'id-token: write',
        'contents: write',
        'permissions: write',
        'secrets: inherit',
        '${{ secrets.',
        'pull_request_target:',
        'workflow_run:',
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.workflows = {
            path.name: path.read_text(encoding='utf-8')
            for path in sorted(WORKFLOW_DIR.glob('*.yml'))
        }

    def test_exactly_the_two_documented_workflows_exist(self):
        self.assertEqual(
            tuple(sorted(self.workflows)),
            tuple(sorted(self.EXPECTED_WORKFLOWS)),
        )

    def test_every_workflow_reads_the_commit_and_persists_no_credential(self):
        for name, text in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertIn('permissions:', text)
                self.assertIn('contents: read', text)
                self.assertIn('persist-credentials: false', text)

    def test_no_workflow_holds_a_deploy_or_credential_capability(self):
        for name, text in self.workflows.items():
            for capability in self.FORBIDDEN_CAPABILITY_SYNTAX:
                with self.subTest(workflow=name, capability=capability):
                    self.assertNotIn(capability, text)
