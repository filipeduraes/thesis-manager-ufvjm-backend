"""HTTP API of the reporting service."""

from django.urls import path

from accounts.views import HealthView
from reports.urls import urlpatterns as report_patterns

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    *report_patterns,
]
