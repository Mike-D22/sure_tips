import asyncio
import aiohttp
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime
from decouple import config
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import re

API_BASE_URL = config('SCRAPE_URL')

def extract_team_from_style(style_str: str) -> str:
    """Extract team name from background-image URL style."""
    match = re.search(r'background-image:url\(&quot;([^&]+)&quot;\)', style_str)
    if match:
        url = match.group(1)
        # Extract team name from URL like /wp-content/themes/freesupertips/image/team/Getafe.png
        team_name = url.split('/')[-1].replace('.png', '').replace('.jpg', '')
        # Handle special cases like "Inter Milan" (space becomes %20 in URL)
        team_name = team_name.replace('%20', ' ')
        return team_name
    return ""


def parse_leg(leg_div, date_text: str = "") -> dict | None:
    """
    Extract match data from a single Leg div.
    Used for accumulator pages (multiple legs per card).
    """
    try:
        # Get time
        time_tag = leg_div.find('time')
        match_time = time_tag.get_text(strip=True) if time_tag else ""
        
        # Extract teams from image backgrounds
        teams_div = leg_div.find('div', class_='Teams')
        teams = []
        if teams_div:
            team_imgs = teams_div.find_all('div', class_='Img Team Team--xs')
            for img in team_imgs:
                style = img.get('style', '')
                team_name = extract_team_from_style(style)
                if team_name:
                    teams.append(team_name)
        
        # Get prediction and opponent
        leg_title = leg_div.find('div', class_='Leg__title')
        prediction = ""
        opponent_text = ""
        if leg_title:
            win_div = leg_title.find('div', class_='Leg__win')
            prediction = win_div.get_text(strip=True) if win_div else ""
            
            lose_div = leg_title.find('div', class_='Leg__lose')
            if lose_div:
                opponent_text = lose_div.get_text(strip=True)
                # Extract opponent from text like "vs Osasuna" or "at Real Betis"
                if 'vs ' in opponent_text:
                    opponent = opponent_text.split('vs ')[-1].strip()
                    if len(teams) < 2:
                        teams.append(opponent)
                elif 'at ' in opponent_text:
                    opponent = opponent_text.split('at ')[-1].strip()
                    if len(teams) < 2:
                        teams.append(opponent)
        
        # Get tip reason
        tip_reason = ""
        collapse_div = leg_div.find('div', class_='Exp-collapse')
        if collapse_div:
            reason_body = collapse_div.find('div', class_='TipReason__body')
            if reason_body:
                p_tag = reason_body.find('p')
                if p_tag:
                    tip_reason = p_tag.get_text(strip=True)
        
        # Get match URL if available
        match_url = ""
        reason_header = leg_div.find('div', class_='TipReason__header')
        if reason_header:
            link = reason_header.find('a', class_='TipReason__link')
            if link and link.get('href'):
                match_url = link.get('href')
        
        # Build match title from teams
        match_title = " vs ".join(teams) if len(teams) >= 2 else prediction
        
        return {
            'date': date_text,
            'time': match_time,
            'match_title': match_title,
            'teams': teams,
            'prediction': prediction,
            'opponent_text': opponent_text,
            'tip_reason': tip_reason,
            'match_url': match_url,
        }
    except Exception as e:
        print(f"Error extracting match from leg: {e}")
        return None


def parse_card(card_div, default_date: str = "") -> dict:
    """
    Parse a single Card div (contains header, multiple legs, and bet grid).
    Returns a dict with tip_category, matches, and accumulator odds.
    """
    try:
        # Get tip category from header
        header = card_div.find('header', class_='TipHeader')
        tip_category = ""
        if header:
            h2 = header.find('h2')
            if h2:
                tip_category = h2.get_text(strip=True)
        
        # Parse all legs in this card
        matches = []
        legs = card_div.find_all('div', class_='Leg')
        for leg in legs:
            match_data = parse_leg(leg, default_date)
            if match_data:
                # Remove odds from individual matches - they don't have their own odds
                matches.append(match_data)
        
        # Extract accumulator-level odds/returns
        stake = None
        returns = None
        total_odds = None
        
        bet_grid = card_div.find('div', class_='BetGrid')
        if bet_grid:
            # Get the active stake value
            select = bet_grid.find('select', {'aria-label': 'select your stake'})
            if select:
                active_option = select.find('option', class_='active')
                if active_option:
                    stake_value = active_option.get('value', '')
                    if stake_value:
                        stake = float(stake_value)
            
            # Get returns amount
            returns_div = bet_grid.find('div', class_='BetGrid__returns')
            if returns_div:
                returns_text = returns_div.get_text(strip=True)
                # Extract number from text like "£2,856.90" or "£34"
                returns_match = re.search(r'[\d,]+\.?\d*', returns_text)
                if returns_match:
                    returns = float(returns_match.group().replace(',', ''))
            
            # Calculate accumulator odds if we have both stake and returns
            if stake and returns and stake > 0:
                total_odds = round(returns / stake, 2)
        
        return {
            'tip_category': tip_category,
            'stake': stake,
            'returns': returns,
            'total_odds': total_odds,  # This is the combined accumulator odds
            'matches': matches,
            'count': len(matches)
        }
    except Exception as e:
        print(f"Error parsing card: {e}")
        return {'tip_category': '', 'matches': [], 'count': 0}

def parse_bet_of_the_day_page(html: bytes) -> dict:
    """
    Parse the Bet of the Day page (multiple tips from different tipsters).
    URL: /bet-of-the-day-tips/
    """
    soup = BeautifulSoup(html, 'html.parser')
    
    # Get current date
    current_date = datetime.now().strftime('%Y-%m-%d')
    
    # Find ALL Card divs (multiple tips on the page)
    cards = soup.find_all('div', class_='Card')
    
    if not cards:
        return {'error': 'No tip cards found', 'matches': [], 'count': 0}
    
    all_matches = []
    # all_tips_summary = []
    
    for card_index, card in enumerate(cards):
        # Parse each card
        card_data = parse_card(card, current_date)
        
        if card_data['matches']:
            tip_category = card_data.get('tip_category', f'Tip {card_index + 1}')
            
            # Add metadata to each match in this card
            for match in card_data['matches']:
                match['tip_category'] = tip_category
                match['stake'] = card_data.get('stake')
                match['returns'] = card_data.get('returns')
                match['odds'] = card_data.get('total_odds')  # Calculated decimal odds
            
            all_matches.extend(card_data['matches'])
            
            # Keep summary of each tip card
            # all_tips_summary.append({
            #     'category': tip_category,
            #     'stake': card_data.get('stake'),
            #     'returns': card_data.get('returns'),
            #     'odds': card_data.get('total_odds'),
            #     'matches_count': len(card_data['matches']),
            #     'predictions': [m.get('prediction') for m in card_data['matches']]
            # })
    
    return {
        'date': current_date,
        'total_tips': len(all_matches),
        'total_cards': len(cards),
        # 'tips_summary': all_tips_summary,
        'matches': all_matches,
        'count': len(all_matches),
        'source': 'freesupertips'
    }

def parse_accumulator_page(html: bytes) -> dict:
    """
    Parse Accumulator Tips page (multiple tips per card).
    URL: /accumulator-tips/
    Returns each accumulator category as a separate object with its matches and odds.
    """
    soup = BeautifulSoup(html, 'html.parser')
    current_date = datetime.now().strftime('%Y-%m-%d')
    
    # Find all Card divs (each card is an accumulator category)
    cards = soup.find_all('div', class_='Card')
    
    if not cards:
        return {'error': 'No accumulator cards found', 'accumulators': [], 'count': 0}
    
    accumulators = []
    
    for card in cards:
        card_data = parse_card(card, current_date)
        
        if card_data['matches']:
            # Get the tip category (this is the accumulator name)
            tip_category = card_data.get('tip_category', 'Unknown Accumulator')
            
            # Create accumulator object (don't add odds to individual matches)
            accumulator = {
                'category': tip_category,
                'stake': card_data.get('stake'),
                'returns': card_data.get('returns'),
                'total_odds': card_data.get('total_odds'),  # Only at accumulator level
                'matches': card_data['matches'],  # Matches without individual odds
                'matches_count': len(card_data['matches'])
            }
            accumulators.append(accumulator)
    
    return {
        'date': current_date,
        'total_accumulators': len(accumulators),
        'accumulators': accumulators,
        'count': sum(acc['matches_count'] for acc in accumulators),
        'source': 'freesupertips'
    }

def parse_generic_tips_page(html: bytes, tip_type: str) -> dict:
    """
    Generic parser for pages with multiple tips.
    Handles: over-2-5-goals, both-teams-to-score, btts-and-win, anytime-goalscorer
    Each page can have multiple accumulator cards, each with its own odds.
    """
    soup = BeautifulSoup(html, 'html.parser')
    current_date = datetime.now().strftime('%Y-%m-%d')
    
    # Find all Card divs (each card is an accumulator/tip category)
    cards = soup.find_all('div', class_='Card')
    
    if not cards:
        return {'error': 'No tip cards found', 'accumulators': [], 'count': 0}
    
    accumulators = []
    
    for card in cards:
        card_data = parse_card(card, current_date)
        
        if card_data['matches']:
            # Get the tip category (e.g., "Both Teams to Score Tips", "Over 2.5 Goals Tips")
            tip_category = card_data.get('tip_category', tip_type)
            
            # Create accumulator object with its own odds
            accumulator = {
                'category': tip_category,
                'tip_type': tip_type,
                'stake': card_data.get('stake'),
                'returns': card_data.get('returns'),
                'total_odds': card_data.get('total_odds'),  # Calculated for this accumulator
                'matches': card_data['matches'],
                'matches_count': len(card_data['matches'])
            }
            accumulators.append(accumulator)
    
    return {
        'date': current_date,
        'tip_type': tip_type,
        'total_accumulators': len(accumulators),
        'accumulators': accumulators,
        'count': sum(acc['matches_count'] for acc in accumulators),
        'source': 'freesupertips'
    }

# ---------------------------------------------------------------------------
# Scraper configuration table
# ---------------------------------------------------------------------------

SCRAPER_CONFIGS = {
    'bet_of_the_day': {
        'path': '/bet-of-the-day-tips/',
        'parser': parse_bet_of_the_day_page,
        'is_accumulator': False,
    },
    'daily_accumulator': {
        'path': '/accumulator-tips/',
        'parser': parse_accumulator_page,
        'is_accumulator': True,
    },
    'over_25_goals': {
        'path': '/over-2-5-goals-betting-tips-and-predictions/',
        'parser': lambda html: parse_generic_tips_page(html, 'over_2.5_goals'),
        'is_accumulator': True,
    },
    'both_teams_to_score': {
        'path': '/both-teams-to-score-tips/',
        'parser': lambda html: parse_generic_tips_page(html, 'btts'),
        'is_accumulator': True,
    },
    'btts_and_win': {
        'path': '/both-teams-to-score-and-win-accumulator/',
        'parser': lambda html: parse_generic_tips_page(html, 'btts_and_win'),
        'is_accumulator': True,
    },
    'anytime_goalscorer': {
        'path': '/anytime-goalscorer-predictions-betting-tips/',
        'parser': lambda html: parse_generic_tips_page(html, 'anytime_goalscorer'),
        'is_accumulator': True,
    },
}


# ---------------------------------------------------------------------------
# Fast concurrent fetcher
# ---------------------------------------------------------------------------

def _fetch_one(config_key: str) -> dict:
    """Fetch + parse a single URL synchronously (runs in thread pool)."""
    scraper = cloudscraper.create_scraper()
    config = SCRAPER_CONFIGS[config_key]
    url = f"{API_BASE_URL}{config['path']}"
    
    try:
        resp = scraper.get(url, timeout=15)
        if not resp or not resp.ok:
            return {'error': f'Failed to fetch {url}', 'status_code': resp.status_code if resp else None}
        
        # Use the specific parser for this page type
        result = config['parser'](resp.content)
        result['scraped_at'] = datetime.now().isoformat()
        result['source_url'] = url
        return result
    except Exception as e:
        return {'error': f'Exception fetching {url}: {str(e)}'}
    finally:
        scraper.close()


def scrape_all(keys: list[str] | None = None) -> dict[str, dict]:
    """
    Scrape all (or a subset of) pages concurrently using a thread pool.
    
    Returns a dict keyed by scraper name.
    
    Example:
        results = scrape_all()
        results = scrape_all(['bet_of_the_day', 'daily_accumulator'])
    """
    keys_to_scrape = keys or list(SCRAPER_CONFIGS.keys())
    
    with ThreadPoolExecutor(max_workers=len(keys_to_scrape)) as pool:
        futures = {
            name: pool.submit(_fetch_one, name)
            for name in keys_to_scrape
        }
        return {name: fut.result() for name, fut in futures.items()}


def scrape_one(key: str) -> dict:
    """Scrape a single page by config key."""
    if key not in SCRAPER_CONFIGS:
        return {'error': f'Unknown scraper key: {key}'}
    return _fetch_one(key)


# ---------------------------------------------------------------------------
# Legacy class wrappers (for compatibility with your existing handler pattern)
# ---------------------------------------------------------------------------

class BetOfTheDayScraper:
    def scrape_bet_of_the_day(self):
        return scrape_one('bet_of_the_day')


class DailyAccumulatorScraper:
    def scrape_daily_accumulator(self):
        return scrape_one('daily_accumulator')


class Over25GoalsScraper:
    def scrape_over_25_goals(self):
        return scrape_one('over_25_goals')


class BothTeamsToScoreScraper:
    def scrape_both_teams_to_score(self):
        return scrape_one('both_teams_to_score')


class BTTSAndWinScraper:
    def scrape_btts_and_win(self):
        return scrape_one('btts_and_win')


class AnytimeGoalscorerScraper:
    def scrape_anytime_goalscorer(self):
        return scrape_one('anytime_goalscorer')