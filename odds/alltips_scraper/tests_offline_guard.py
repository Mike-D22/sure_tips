"""Network-isolation proof for the parser contract tests.

Why this module exists
----------------------
The parser contract tests added for the fixture work are only meaningful if they
are genuinely offline - a "fixture" test that quietly made a live request would
be non-deterministic and could hit the real source. This module proves the
isolation from both directions:

1. Inside-out. The socket layer is disabled for every test in this module, and
   ``SocketIsBlockedForParserTests`` demonstrates that the guard really does
   block connections and DNS lookups, so the remaining tests prove something.
2. Outside-in. The real fetch path (``utils.scrape_one`` / ``utils.scrape_all``)
   is exercised with an HTTP client that always fails, proving the scraper layer
   fails closed with an error envelope instead of raising.

No test here can reach the network: ``OfflineGuardMixin`` is applied to every
class and the failing HTTP client is a ``unittest.mock`` object, so no socket is
ever requested by the production code path under test.

See ``docs/DATA_CONTRACT.md`` for the documented contract.
"""

import socket
from unittest import mock

from django.test import SimpleTestCase

from . import utils
from .tests_parser_contract import (
    FIXTURE_FOR_SOURCE,
    OfflineGuardMixin,
    load_fixture,
)


class SocketIsBlockedForParserTests(OfflineGuardMixin, SimpleTestCase):
    """The guard has to be effective, or the other tests prove nothing."""

    def test_outbound_socket_creation_is_blocked(self):
        with self.assertRaises(AssertionError):
            socket.socket()

    def test_outbound_connections_are_blocked(self):
        with self.assertRaises(AssertionError):
            socket.create_connection(('parser-tests-must-not-connect.invalid', 80))

    def test_dns_resolution_is_blocked(self):
        with self.assertRaises(AssertionError):
            socket.getaddrinfo('parser-tests-must-not-connect.invalid', 80)


class ParsersRunEntirelyOfflineTests(OfflineGuardMixin, SimpleTestCase):
    """All six source parsers must complete with the socket layer disabled."""

    def test_all_six_source_parsers_produce_payloads_with_sockets_disabled(self):
        for key, config in utils.SCRAPER_CONFIGS.items():
            with self.subTest(source=key):
                result = config['parser'](load_fixture(FIXTURE_FOR_SOURCE[key]))
                self.assertTrue('error' not in result)
                self.assertGreater(result['count'], 0)

    def test_fixtures_are_read_from_local_disk(self):
        for name in sorted(FIXTURE_FOR_SOURCE.values()):
            with self.subTest(fixture=name):
                self.assertTrue(b'<html' in load_fixture(name).lower())

    def test_parsing_never_asks_for_an_http_client(self):
        with mock.patch.object(utils.cloudscraper, 'create_scraper') as create_scraper:
            for key, config in utils.SCRAPER_CONFIGS.items():
                with self.subTest(source=key):
                    config['parser'](load_fixture(FIXTURE_FOR_SOURCE[key]))
        create_scraper.assert_not_called()


class ScraperFetchPathFailsClosedTests(OfflineGuardMixin, SimpleTestCase):
    """The real fetch path must return an error envelope, never raise."""

    @staticmethod
    def _failing_scraper():
        scraper = mock.Mock()
        scraper.get.side_effect = OSError('network disabled by the offline guard')
        return scraper

    def test_scrape_one_returns_an_error_envelope(self):
        with mock.patch.object(
            utils.cloudscraper, 'create_scraper', return_value=self._failing_scraper()
        ):
            result = utils.scrape_one('bet_of_the_day')
        self.assertTrue('error' in result)
        # assertTrue keeps the URL-bearing message out of failure output.
        self.assertTrue(result['error'].startswith('Exception fetching'))

    def test_scrape_one_closes_the_http_client_even_when_it_fails(self):
        scraper = self._failing_scraper()
        with mock.patch.object(
            utils.cloudscraper, 'create_scraper', return_value=scraper
        ):
            utils.scrape_one('daily_accumulator')
        scraper.close.assert_called_once()

    def test_scrape_one_never_builds_a_client_for_an_unknown_key(self):
        with mock.patch.object(utils.cloudscraper, 'create_scraper') as create_scraper:
            result = utils.scrape_one('not-a-source')
        self.assertEqual(result, {'error': 'Unknown scraper key: not-a-source'})
        create_scraper.assert_not_called()

    def test_scrape_all_reports_one_error_per_requested_source(self):
        with mock.patch.object(
            utils.cloudscraper, 'create_scraper', return_value=self._failing_scraper()
        ):
            results = utils.scrape_all(['bet_of_the_day', 'daily_accumulator'])
        self.assertEqual(set(results), {'bet_of_the_day', 'daily_accumulator'})
        for key, result in results.items():
            with self.subTest(source=key):
                self.assertTrue('error' in result)

    def test_the_legacy_scraper_wrappers_stay_offline_too(self):
        wrappers = (
            (utils.BetOfTheDayScraper().scrape_bet_of_the_day, 'bet_of_the_day'),
            (utils.DailyAccumulatorScraper().scrape_daily_accumulator,
             'daily_accumulator'),
            (utils.Over25GoalsScraper().scrape_over_25_goals, 'over_25_goals'),
            (utils.BothTeamsToScoreScraper().scrape_both_teams_to_score,
             'both_teams_to_score'),
            (utils.BTTSAndWinScraper().scrape_btts_and_win, 'btts_and_win'),
            (utils.AnytimeGoalscorerScraper().scrape_anytime_goalscorer,
             'anytime_goalscorer'),
        )
        for call, source in wrappers:
            with self.subTest(source=source), mock.patch.object(
                utils.cloudscraper, 'create_scraper',
                return_value=self._failing_scraper(),
            ):
                result = call()
            self.assertTrue('error' in result)
