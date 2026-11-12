from rest_framework.throttling import UserRateThrottle, AnonRateThrottle

class SubscriptionStatusThrottle(UserRateThrottle):
    """Throttle for subscription status checks"""
    scope = 'subscription_status'
    rate = '60/hour'  # 60 requests per hour

class VerifySubscriptionThrottle(UserRateThrottle):
    """Throttle for subscription verification"""
    scope = 'verify_subscription'
    rate = '30/hour'  # 30 requests per hour

class AnonymousUserThrottle(AnonRateThrottle):
    """Throttle for anonymous user creation"""
    scope = 'anonymous_user'
    rate = '10/hour'  # 10 requests per hour from same IP

class RevenueCatWebhookThrottle(AnonRateThrottle):
    """Throttle for RevenueCat webhooks"""
    scope = 'revenuecat_webhook'
    rate = '100/hour'


from rest_framework.exceptions import Throttled
from rest_framework.views import exception_handler

def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    
    if isinstance(exc, Throttled):
        response.data = {
            'error': 'Rate limit exceeded',
            'detail': f'Please try again in {exc.wait} seconds',
            'available_in': exc.wait,
            'limit_type': getattr(exc, 'scope', 'unknown')
        }
    
    return response