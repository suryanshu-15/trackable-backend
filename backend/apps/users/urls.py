"""
URL patterns for auth and user management.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView, TokenBlacklistView

from .views import (
    TrackableTokenObtainPairView,
    MeView,
    ChangePasswordView,
    UserViewSet,
)

router = DefaultRouter()
router.register('users', UserViewSet, basename='user')

urlpatterns = [
    # Auth
    path('login/', TrackableTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', TokenBlacklistView.as_view(), name='token_blacklist'),
    path('me/', MeView.as_view(), name='me'),
    path('change-password/', ChangePasswordView.as_view(), name='change_password'),
    # User management
    path('', include(router.urls)),
]
