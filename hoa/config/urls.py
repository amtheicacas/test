from django.contrib import admin
from django.urls import include, path

from violations.security_views import ResetCompleteView, ResetConfirmView, ResetDoneView, ThrottledPasswordResetView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/password_reset/", ThrottledPasswordResetView.as_view(), name="password_reset"),
    path("accounts/password_reset/done/", ResetDoneView.as_view(), name="password_reset_done"),
    path("accounts/reset/<uidb64>/<token>/", ResetConfirmView.as_view(), name="password_reset_confirm"),
    path("accounts/reset/done/", ResetCompleteView.as_view(), name="password_reset_complete"),
    path("accounts/", include("django.contrib.auth.urls")),  # password reset / invite links
    path("", include("violations.urls")),
]
