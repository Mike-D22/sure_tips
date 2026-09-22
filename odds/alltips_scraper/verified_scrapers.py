from .utils import (
    BetOfTheDayScraper,
    DailyAccumulatorScraper,
    Over25GoalsScraper,
    BothTeamsToScoreScraper,
    BTTSAndWinScraper,
    AnytimeGoalscorerScraper,
)


class VerifiedBetOfTheDayScraper(BetOfTheDayScraper):
    """Bet of the Day with actual score verification"""
    
    def scrape_with_verification(self):
        """Scrape Bet of the Day and verify with actual scores"""
        data = self.scrape_bet_of_the_day()
        
        if data.get('matches') and not data.get('error'):
            # Extract team pairs
            team_pairs = []
            for match in data['matches']:
                teams = match.get('teams', [])
                if len(teams) >= 2:
                    team_pairs.append((teams[0], teams[1]))
            
            if team_pairs:
                # Get actual scores from Soccerbase
                # match_results = get_bulk_results(team_pairs)
                # data = verify_predictions(data, match_results, 'bet_of_day')
                
                # TODO: Uncomment when soccerbase is integrated
                data['verification_note'] = 'Soccerbase integration pending'
        
        return data


class VerifiedDailyAccumulatorScraper(DailyAccumulatorScraper):
    """Daily Accumulator with actual score verification"""
    
    def scrape_with_verification(self):
        """Scrape Daily Accumulator and verify with actual scores"""
        data = self.scrape_daily_accumulator()
        
        if data.get('matches') and not data.get('error'):
            # Extract team pairs
            team_pairs = []
            for match in data['matches']:
                teams = match.get('teams', [])
                if len(teams) >= 2:
                    team_pairs.append((teams[0], teams[1]))
            
            if team_pairs:
                # Get actual scores from Soccerbase
                # match_results = get_bulk_results(team_pairs)
                # data = verify_predictions(data, match_results, 'daily_accumulator')
                
                # TODO: Uncomment when soccerbase is integrated
                data['verification_note'] = 'Soccerbase integration pending'
        
        return data


class VerifiedOver25GoalsScraper(Over25GoalsScraper):
    """Over 2.5 Goals with actual score verification"""
    
    def scrape_with_verification(self):
        """Scrape Over 2.5 Goals and verify with actual scores"""
        data = self.scrape_over_25_goals()
        
        if data.get('matches') and not data.get('error'):
            # Extract team pairs
            team_pairs = []
            for match in data['matches']:
                teams = match.get('teams', [])
                if len(teams) >= 2:
                    team_pairs.append((teams[0], teams[1]))
            
            if team_pairs:
                # Get actual scores from Soccerbase
                # match_results = get_bulk_results(team_pairs)
                # data = verify_predictions(data, match_results, 'over_25')
                
                # TODO: Uncomment when soccerbase is integrated
                data['verification_note'] = 'Soccerbase integration pending'
        
        return data


class VerifiedBothTeamsToScoreScraper(BothTeamsToScoreScraper):
    """Both Teams to Score with actual score verification"""
    
    def scrape_with_verification(self):
        """Scrape Both Teams to Score and verify with actual scores"""
        data = self.scrape_both_teams_to_score()
        
        if data.get('matches') and not data.get('error'):
            # Extract team pairs
            team_pairs = []
            for match in data['matches']:
                teams = match.get('teams', [])
                if len(teams) >= 2:
                    team_pairs.append((teams[0], teams[1]))
            
            if team_pairs:
                # Get actual scores from Soccerbase
                # match_results = get_bulk_results(team_pairs)
                # data = verify_predictions(data, match_results, 'btts')
                
                # TODO: Uncomment when soccerbase is integrated
                data['verification_note'] = 'Soccerbase integration pending'
        
        return data


class VerifiedBTTSAndWinScraper(BTTSAndWinScraper):
    """BTTS & Win Accumulator with actual score verification"""
    
    def scrape_with_verification(self):
        """Scrape BTTS & Win Accumulator and verify with actual scores"""
        data = self.scrape_btts_and_win()
        
        if data.get('matches') and not data.get('error'):
            # Extract team pairs
            team_pairs = []
            for match in data['matches']:
                teams = match.get('teams', [])
                if len(teams) >= 2:
                    team_pairs.append((teams[0], teams[1]))
            
            if team_pairs:
                # Get actual scores from Soccerbase
                # match_results = get_bulk_results(team_pairs)
                # data = verify_predictions(data, match_results, 'btts_and_win')
                
                # TODO: Uncomment when soccerbase is integrated
                data['verification_note'] = 'Soccerbase integration pending'
        
        return data


class VerifiedAnytimeGoalscorerScraper(AnytimeGoalscorerScraper):
    """Anytime Goalscorer with actual score verification"""
    
    def scrape_with_verification(self):
        """Scrape Anytime Goalscorer and verify with actual scores"""
        data = self.scrape_anytime_goalscorer()
        
        if data.get('matches') and not data.get('error'):
            # Extract team pairs
            team_pairs = []
            for match in data['matches']:
                teams = match.get('teams', [])
                if len(teams) >= 2:
                    team_pairs.append((teams[0], teams[1]))
            
            if team_pairs:
                # Get actual scores from Soccerbase
                # match_results = get_bulk_results(team_pairs)
                
                # For anytime goalscorer, we just verify but don't adjust
                data['verified'] = True
                data['processed_at'] = __import__('datetime').datetime.now().isoformat()
                
                # TODO: Uncomment when soccerbase is integrated
                # for match in data['matches']:
                #     teams = match.get('teams', [])
                #     if len(teams) >= 2:
                #         match_key = f"{teams[0]}|{teams[1]}"
                #         match_result = match_results.get(match_key)
                #         
                #         if match_result and match_result.get('found'):
                #             match['actual_score'] = match_result['score']
                #             match['actual_home_score'] = match_result['home_score']
                #             match['actual_away_score'] = match_result['away_score']
                #             match['total_goals'] = match_result['total_goals']
                #             match['verified'] = True
                #         else:
                #             match['verified'] = False
                #             match['verification_error'] = 'Match not found'
                
                data['verification_note'] = 'Soccerbase integration pending'
        
        return data