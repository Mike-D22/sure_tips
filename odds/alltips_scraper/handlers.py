from .utils import (
    AnytimeGoalscorerScraper,
    BetOfTheDayScraper,
    BothTeamsToScoreScraper,
    DailyAccumulatorScraper,
    Over25GoalsScraper,
    BTTSAndWinScraper,
)
import logging
from datetime import datetime

from decouple import config

logger = logging.getLogger(__name__)

# Result/settlement verification is not implemented yet (Soccerbase integration
# is pending), so it stays disabled unless explicitly enabled in the environment.
# The value itself is never logged.
ENABLE_VERIFICATION = config('FREESUPERTIPS_ENABLE_VERIFICATION', default=False, cast=bool)


def get_bet_of_the_day():
    """Get today's Bet of the Day from FreeSuperTips"""
    scraper = BetOfTheDayScraper()
    data = scraper.scrape_bet_of_the_day()
    
    if ENABLE_VERIFICATION and data.get('matches') and not data.get('error'):
        data = _verify_predictions(data, 'bet_of_day')
    
    return data


def get_daily_accumulator():
    """Get today's Daily Accumulator Tips from FreeSuperTips"""
    scraper = DailyAccumulatorScraper()
    data = scraper.scrape_daily_accumulator()
    
    if ENABLE_VERIFICATION and data.get('matches') and not data.get('error'):
        data = _verify_predictions(data, 'daily_accumulator')
    
    return data


def get_over_25_goals_accumulator():
    """Get today's Over 2.5 Goals Tips from FreeSuperTips"""
    scraper = Over25GoalsScraper()
    data = scraper.scrape_over_25_goals()
    
    if ENABLE_VERIFICATION and data.get('matches') and not data.get('error'):
        logger.debug("Verification enabled for over/under 2.5 goals tips")
        data = _verify_predictions(data, 'over_25')
    else:
        logger.debug(
            "Verification skipped for over/under 2.5 goals tips "
            "(disabled, no matches, or error payload)"
        )
    
    return data


def get_both_teams_to_score():
    """Get today's Both Teams to Score Tips from FreeSuperTips"""
    scraper = BothTeamsToScoreScraper()
    data = scraper.scrape_both_teams_to_score()
    
    if ENABLE_VERIFICATION and data.get('matches') and not data.get('error'):
        data = _verify_predictions(data, 'btts')
    
    return data


def get_btts_win_accumulator():
    """Get today's Both Teams to Score and Win Accumulator Tips from FreeSuperTips"""
    scraper = BTTSAndWinScraper()
    data = scraper.scrape_btts_and_win()
    
    if ENABLE_VERIFICATION and data.get('matches') and not data.get('error'):
        data = _verify_predictions(data, 'btts_and_win')
    
    return data


def get_anytime_goalscorer():
    """Get today's Anytime Goalscorer Tips from FreeSuperTips"""
    scraper = AnytimeGoalscorerScraper()
    data = scraper.scrape_anytime_goalscorer()
    
    if ENABLE_VERIFICATION and data.get('matches') and not data.get('error'):
        data = _add_scores_to_goalscorer(data)
    
    return data


def _verify_predictions(data, accumulator_type):
    """Helper function to verify predictions with actual scores."""
    team_pairs = []
    for match in data.get('matches', []):
        teams = match.get('teams', [])
        if len(teams) >= 2:
            team_pairs.append((teams[0], teams[1]))
        elif len(teams) == 1 and ' vs ' in match.get('match_title', ''):
            title_parts = match['match_title'].split(' vs ')
            if len(title_parts) >= 2:
                team_pairs.append((title_parts[0], title_parts[1]))
                match['teams'] = [title_parts[0], title_parts[1]]
    
    if not team_pairs:
        data['verification_note'] = 'No team pairs found for verification'
        return data
    
    # TODO: Uncomment when soccerbase is integrated
    data['verification_enabled'] = True
    data['verification_note'] = 'Soccerbase integration pending'
    data['verified'] = False
    
    for match in data.get('matches', []):
        match['verified'] = False
        match['verification_status'] = 'pending'
    
    return data


def _add_scores_to_goalscorer(data):
    """Helper function to add actual scores to anytime goalscorer predictions."""
    team_pairs = []
    for match in data.get('matches', []):
        teams = match.get('teams', [])
        if len(teams) >= 2:
            team_pairs.append((teams[0], teams[1]))
        elif len(teams) == 1 and ' vs ' in match.get('match_title', ''):
            title_parts = match['match_title'].split(' vs ')
            if len(title_parts) >= 2:
                team_pairs.append((title_parts[0], title_parts[1]))
                match['teams'] = [title_parts[0], title_parts[1]]
    
    if not team_pairs:
        data['verification_note'] = 'No team pairs found for verification'
        return data
    
    data['verification_enabled'] = True
    data['verification_note'] = 'Soccerbase integration pending - scores not added'
    data['verified'] = False
    
    return data


def get_all_tips():
    """Get all tips from FreeSuperTips in one call"""
    return {
        'bet_of_the_day': get_bet_of_the_day(),
        'daily_accumulator': get_daily_accumulator(),
        'over_25_goals': get_over_25_goals_accumulator(),
        'both_teams_to_score': get_both_teams_to_score(),
        'btts_win_accumulator': get_btts_win_accumulator(),
        'anytime_goalscorer': get_anytime_goalscorer(),
        'scraped_at': datetime.now().isoformat(),
        'source': 'freesupertips.com'
    }