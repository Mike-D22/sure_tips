"""Response caching for the legacy (unversioned) tips endpoints.

The six legacy endpoints are public and unversioned, so this decorator is
written so that caching can never change the shape of their JSON envelope:

* the payload is cached exactly as the view returned it (never re-wrapped, and
  never re-keyed under ``matches``);
* a cache hit returns the stored payload unchanged with ``cached: True``, a
  miss returns the view payload unchanged with ``cached: False``;
* error payloads (any top-level ``error`` key) are never cached;
* the cache key contains the view identity and the current timezone-aware date,
  so a response cached before midnight can never be served after midnight;
* the cache key also fingerprints the query string, so adding query parameters
  to an endpoint later cannot cause cross-parameter cache collisions.
"""

import hashlib
import json
from functools import wraps

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone

CACHE_KEY_PREFIX = 'legacy_api:v1'
DEFAULT_CACHE_TIMEOUT = 14400  # 4 hours, unchanged from the original decorator


def _query_string_fingerprint(request):
    """Return a short stable fingerprint of the query string ('' when empty)."""
    query_string = getattr(request, 'GET', None)
    if not query_string:
        return ''
    pairs = '&'.join(f'{key}={value}' for key, value in sorted(query_string.items()))
    return hashlib.sha1(pairs.encode('utf-8')).hexdigest()[:12]


def build_cache_key(view_name, request=None, today=None):
    """Build the cache key for a legacy view.

    ``today`` is injectable so tests can exercise date rollover deterministically.
    """
    if today is None:
        today = timezone.localdate()
    key = f'{CACHE_KEY_PREFIX}:{view_name}:{today.isoformat()}'
    fingerprint = _query_string_fingerprint(request)
    if fingerprint:
        key = f'{key}:{fingerprint}'
    return key


def _as_response(payload, cached):
    """Serialise a payload with the legacy ``cached`` flag applied."""
    body = dict(payload)
    body['cached'] = cached
    return JsonResponse(body)


def cache_matches(timeout=DEFAULT_CACHE_TIMEOUT):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            cache_key = build_cache_key(view_func.__name__, request)

            cached_payload = cache.get(cache_key)
            if isinstance(cached_payload, dict):
                return _as_response(cached_payload, cached=True)

            response = view_func(request, *args, **kwargs)
            payload = json.loads(response.content)

            if not isinstance(payload, dict):
                # Unexpected shape: never cache it, never reshape it.
                return response

            if 'error' not in payload:
                cache.set(cache_key, payload, timeout)

            return _as_response(payload, cached=False)

        return wrapped
    return decorator
