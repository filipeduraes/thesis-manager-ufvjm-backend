"""URL configuration for thesismanagerbackend project."""

from django.contrib import admin
from django.urls import include, path

from accounts.views import HealthView

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", include("accounts.urls")),
    path("", include("academic.urls")),
]
