from django.contrib import admin
from django.urls import include, path

from violations.security_views import ThrottledPasswordResetView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/password_reset/", ThrottledPasswordResetView.as_view(), name="password_reset"),
    path("accounts/", include("django.contrib.auth.urls")),  # password reset / invite links
    path("", include("violations.urls")),
]
