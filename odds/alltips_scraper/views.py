from django.http import JsonResponse
from .decorators import cache_matches
from .handlers import ( 
    get_anytime_goalscorer,
    get_bet_of_the_day,
    get_both_teams_to_score,
    get_daily_accumulator,
    get_btts_win_accumulator,
    get_over_25_goals_accumulator,
)


@cache_matches()
def bet_of_the_day(request):
    """Return Bet of the Day data"""
    result = get_bet_of_the_day()
    return JsonResponse(result)

@cache_matches()
def daily_accumulator(request):
    """Return Daily Accumulator Tips"""
    result = get_daily_accumulator()
    return JsonResponse(result)

@cache_matches()
def btts_win_accumulator(request):
    """Return BTTS and Win Accumulator Tips"""
    result = get_btts_win_accumulator()
    return JsonResponse(result)

@cache_matches()
def over_25_goals_accumulator(request):
    """Return Over 2.5 Goals Accumulator Tips"""
    result = get_over_25_goals_accumulator()
    return JsonResponse(result)

@cache_matches()
def both_teams_to_score(request):
    """Return Both Teams to Score Tips"""
    result = get_both_teams_to_score()
    return JsonResponse(result)

@cache_matches()
def anytime_goalscorer(request):
    """Return Anytime Goalscorer Tips"""
    result = get_anytime_goalscorer()
    return JsonResponse(result)

