"""The versioned API surface: the ``/api/v1/`` routes (Commit 3).

This module is deliberately tiny. It owns the namespace and the one route the
versioned surface publishes today, so the legacy routes in ``odds/urls.py`` stay
exactly as they are and a future version can be added beside it instead of inside
it. The route name is namespaced (``tips_v1:tips``), so a caller can build the URL
without knowing the path, and the app name is the namespace.

The view it points at lives in ``views_v1.py``; nothing else is imported here, and
this module must stay free of the scraper, the transport layer and the settings so
that importing the URL configuration can never start a fetch or read a secret.
"""

from django.urls import path

from .views_v1 import tips_v1

app_name = 'tips_v1'

urlpatterns = [
    path('tips/', tips_v1, name='tips'),
]
