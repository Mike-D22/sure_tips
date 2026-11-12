from django.core.cache import cache
from django.http import JsonResponse
from functools import wraps

def cache_matches(timeout=14400):  # 4 hours
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            cache_key = f"{view_func.__name__}"
            
            # # Check cache first
            if cached_data := cache.get(cache_key):
                # Return cached data in the same format as your original view
                if isinstance(cached_data, dict) and 'matches' in cached_data:
                    cached_data['cached'] = True
                    return JsonResponse(cached_data)
                else:
                    return JsonResponse({
                        'matches': cached_data,
                        'message': f'Found {len(cached_data)} cached matches',
                        'available': bool(cached_data),
                        'cached': True
                    })
            
            # Call original function
            response = view_func(request, *args, **kwargs)
            
            # Extract data from response to cache
            import json
            response_data = json.loads(response.content)
            
            # Determine what to cache based on your view's structure
            if isinstance(response_data, dict) and 'matches' in response_data:
                cache.set(cache_key, response_data, timeout)  # Cache the whole dict
            else:
                cache.set(cache_key, response_data.get('matches', response_data), timeout)
            
            # Add cache status to response
            response_data['cached'] = False
            return JsonResponse(response_data)
            
        return wrapped
    return decorator