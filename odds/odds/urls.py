# your_project/urls.py
from django.contrib import admin
from django.urls import path
from alltips_scraper.views import (
    # Original scrapers (keeping your existing ones)
    anytime_goalscorer,
    bet_of_the_day,
    daily_accumulator,
    btts_win_accumulator,
    over_25_goals_accumulator,
    both_teams_to_score,
)


urlpatterns = [
    path('admin/', admin.site.urls),
    
    # ==========================================================
    # Original AllTips Scraper Endpoints
    # ==========================================================
    path('api/bet-of-the-day/', bet_of_the_day, name='bet_of_the_day'),
    path('api/daily-accumulator/', daily_accumulator, name='daily_accumulator'),
    path('api/btts-win-accumulator/', btts_win_accumulator, name='btts_win_accumulator'),
    path('api/over-25-goals-accumulator/', over_25_goals_accumulator, name='over_25_goals_accumulator'),
    path('api/BTTS/', both_teams_to_score, name='both_teams_to_score'),
    path('api/goalscorer/', anytime_goalscorer, name='anytime_goalscorer'),

    # ==========================================================
    # Customer Endpoint
    # ==========================================================
    # path('api/contact-us/', contact_us, name='contact-us'),
]