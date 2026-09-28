"""Tests for the legacy (unversioned) tips API and its cache decorator.

These tests never touch the network: every scraper entry point used by the
views is monkeypatched, so they assert the envelope contract only.
"""

from datetime import date
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from .decorators import build_cache_key

# ---------------------------------------------------------------------------
# Representative payloads, mirroring the envelopes the scrapers return.
# ---------------------------------------------------------------------------
BET_OF_THE_DAY_PAYLOAD = {
    'date': '2026-09-27',
    'total_tips': 2,
    'total_cards': 2,
    'matches': [
        {'match_title': 'Arsenal vs Chelsea', 'prediction': 'Arsenal to win'},
        {'match_title': 'Bayern vs Dortmund', 'prediction': 'Over 2.5 goals'},
    ],
    'count': 2,
    'source': 'freesupertips',
}

ACCUMULATOR_PAYLOAD = {
    'date': '2026-09-27',
    'total_accumulators': 1,
    'accumulators': [
        {
            'tip_category': 'Daily Accumulator',
            'stake': '10',
            'returns': '25',
            'total_odds': 2.5,
            'matches': [{'match_title': 'Arsenal vs Chelsea'}],
            'count': 1,
        },
    ],
    'count': 1,
    'source': 'freesupertips',
}

GENERIC_TIPS_PAYLOAD = dict(ACCUMULATOR_PAYLOAD, tip_type='btts')

# (url, view name, handler target used by the view, expected payload)
ENDPOINTS = [
    ('/api/bet-of-the-day/', 'bet_of_the_day',
     'alltips_scraper.views.get_bet_of_the_day', BET_OF_THE_DAY_PAYLOAD),
    ('/api/daily-accumulator/', 'daily_accumulator',
     'alltips_scraper.views.get_daily_accumulator', ACCUMULATOR_PAYLOAD),
    ('/api/btts-win-accumulator/', 'btts_win_accumulator',
     'alltips_scraper.views.get_btts_win_accumulator', ACCUMULATOR_PAYLOAD),
    ('/api/over-25-goals-accumulator/', 'over_25_goals_accumulator',
     'alltips_scraper.views.get_over_25_goals_accumulator', ACCUMULATOR_PAYLOAD),
    ('/api/BTTS/', 'both_teams_to_score',
     'alltips_scraper.views.get_both_teams_to_score', GENERIC_TIPS_PAYLOAD),
    ('/api/goalscorer/', 'anytime_goalscorer',
     'alltips_scraper.views.get_anytime_goalscorer', ACCUMULATOR_PAYLOAD),
]

ACCUMULATOR_ENDPOINTS = [endpoint for endpoint in ENDPOINTS if endpoint[1] != 'bet_of_the_day']
HANDLER_TARGETS = [endpoint[2] for endpoint in ENDPOINTS]


class HealthEndpointTests(TestCase):
    """The operational endpoint must not depend on anything that can fail."""

    def test_health_is_scraper_independent(self):
        patches = [
            mock.patch(target, side_effect=AssertionError(f'{target} must not run'))
            for target in HANDLER_TARGETS
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'status': 'ok', 'service': 'sure-tips-api', 'api_version': 'legacy'},
        )

    def test_health_exposes_no_configuration_or_paths(self):
        response = self.client.get('/api/health/')
        body = response.content.decode('utf-8')

        self.assertEqual(set(response.json()), {'status', 'service', 'api_version'})
        for forbidden in ('SECRET', 'secret', 'sqlite', 'C:',
                          'ALLOWED_HOSTS', 'SCRAPE_URL', 'environ', '.env'):
            self.assertNotIn(forbidden, body)


@override_settings(ALLOWED_HOSTS=['testserver'])
class LegacyCacheContractTests(TestCase):
    """A cache hit must return exactly what a cache miss returned."""

    def setUp(self):
        cache.clear()

    def test_envelope_is_identical_cold_and_warm(self):
        for url, _view_name, target, payload in ENDPOINTS:
            with self.subTest(endpoint=url):
                cache.clear()
                with mock.patch(target, return_value=payload) as handler:
                    cold = self.client.get(url)
                    warm = self.client.get(url)

                self.assertEqual(cold.status_code, 200)
                self.assertEqual(warm.status_code, 200)

                cold_body = cold.json()
                warm_body = warm.json()

                self.assertIs(cold_body['cached'], False)
                self.assertIs(warm_body['cached'], True)
                self.assertEqual(set(cold_body), set(warm_body))
                self.assertEqual(cold_body['count'], payload['count'])
                self.assertEqual(warm_body['count'], payload['count'])

                cold_body.pop('cached')
                warm_body.pop('cached')
                self.assertEqual(cold_body, payload)
                self.assertEqual(warm_body, payload)

                # exactly one scrape call served both requests
                self.assertEqual(handler.call_count, 1)

    def test_accumulator_envelopes_are_never_rewritten(self):
        for url, _view_name, target, payload in ACCUMULATOR_ENDPOINTS:
            with self.subTest(endpoint=url):
                cache.clear()
                with mock.patch(target, return_value=payload):
                    cold_body = self.client.get(url).json()
                    warm_body = self.client.get(url).json()

                for body in (cold_body, warm_body):
                    if 'tip_type' in payload:
                        self.assertEqual(body['tip_type'], payload['tip_type'])
                    self.assertEqual(body['accumulators'], payload['accumulators'])
                    self.assertEqual(body['count'], len(payload['accumulators']))
                    self.assertEqual(body['total_accumulators'], payload['total_accumulators'])
                    # regression guards for the old decorator bug
                    self.assertNotIn('matches', body)
                    self.assertNotIn('message', body)
                    self.assertNotIn('available', body)

    def test_cache_keys_are_namespaced_per_view(self):
        # The date is injected explicitly: the production helper defaults to the
        # timezone-aware date, which must never make this test depend on the
        # clock, the machine locale or the developer's time zone.
        fixed_today = date(2026, 9, 27)
        bet_of_the_day = build_cache_key('bet_of_the_day', today=fixed_today)
        daily_accumulator = build_cache_key('daily_accumulator', today=fixed_today)

        self.assertNotEqual(bet_of_the_day, daily_accumulator)
        self.assertTrue(bet_of_the_day.startswith('legacy_api:v1:bet_of_the_day:'))
        self.assertIn('2026-09-27', bet_of_the_day)


@override_settings(ALLOWED_HOSTS=['testserver'])
class ErrorResponseCacheTests(TestCase):
    """Error payloads must reach the client unchanged and never be cached."""

    def setUp(self):
        cache.clear()

    def test_bet_of_the_day_error_is_not_cached(self):
        url, view_name, target, _payload = ENDPOINTS[0]
        error_payload = {'error': 'No tip cards found', 'matches': [], 'count': 0}

        with mock.patch(target, return_value=error_payload) as handler:
            first = self.client.get(url)
            second = self.client.get(url)

        self.assertEqual(handler.call_count, 2)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()['error'], 'No tip cards found')
        self.assertEqual(first.json()['matches'], [])
        self.assertIs(first.json()['cached'], False)
        self.assertIs(second.json()['cached'], False)
        self.assertIsNone(cache.get(build_cache_key(view_name)))

    def test_accumulator_error_is_not_cached(self):
        url, view_name, target, _payload = ENDPOINTS[2]
        error_payload = {
            'error': 'Failed to fetch page: 503',
            'accumulators': [],
            'count': 0,
        }

        with mock.patch(target, return_value=error_payload) as handler:
            first = self.client.get(url)
            second = self.client.get(url)

        self.assertEqual(handler.call_count, 2)
        self.assertEqual(first.json()['error'], 'Failed to fetch page: 503')
        self.assertEqual(second.json()['accumulators'], [])
        self.assertIs(second.json()['cached'], False)
        self.assertIsNone(cache.get(build_cache_key(view_name)))


@override_settings(ALLOWED_HOSTS=['testserver'])
class CacheKeyDateTests(TestCase):
    """A response cached one day must never be served on the next day."""

    def setUp(self):
        cache.clear()

    def test_cache_key_is_scoped_to_the_current_date(self):
        url, view_name, target, payload = ENDPOINTS[1]
        yesterday = dict(
            payload,
            date='2026-09-26',
            accumulators=[{'tip_category': 'Yesterday accumulator'}],
        )
        today = dict(
            payload,
            date='2026-09-27',
            accumulators=[{'tip_category': 'Today accumulator'}],
        )

        with mock.patch(target, side_effect=[yesterday, today]) as handler:
            with mock.patch('alltips_scraper.decorators.timezone.localdate',
                            return_value=date(2026, 9, 26)):
                first = self.client.get(url).json()
            with mock.patch('alltips_scraper.decorators.timezone.localdate',
                            return_value=date(2026, 9, 27)):
                second = self.client.get(url).json()

        self.assertEqual(handler.call_count, 2)
        self.assertEqual(first['date'], '2026-09-26')
        self.assertEqual(second['date'], '2026-09-27')
        self.assertIs(second['cached'], False)
        self.assertEqual(second['accumulators'], [{'tip_category': 'Today accumulator'}])

        self.assertIsNotNone(cache.get(build_cache_key(view_name, today=date(2026, 9, 26))))
        self.assertIsNotNone(cache.get(build_cache_key(view_name, today=date(2026, 9, 27))))

        # ...while a second request on the same day is served from the cache.
        with mock.patch('alltips_scraper.decorators.timezone.localdate',
                        return_value=date(2026, 9, 27)):
            third = self.client.get(url).json()

        self.assertEqual(handler.call_count, 2)
        self.assertIs(third['cached'], True)
        self.assertEqual(third['date'], '2026-09-27')
