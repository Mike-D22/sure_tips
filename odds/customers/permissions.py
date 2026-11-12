from rest_framework.permissions import BasePermission

class IsAppUser(BasePermission):
    """
    Simple permission class to authenticate users by app_user_id in the headers.
    """
    def has_permission(self, request, view):
        # Expect header: Authorization: AppUser <app_user_id>
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("AppUser "):
            app_user_id = auth_header.replace("AppUser ", "").strip()
            if app_user_id:
                # Save it to request for views
                request.app_user_id = app_user_id
                return True
        return False
