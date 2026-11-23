"""HTTP API of the thesis submission and publication service."""

from django.urls import path

from accounts.views import HealthView
from theses.urls import urlpatterns as thesis_patterns
from thesismanagerbackend.service_urls import patterns_for

COMMITTEE_ROUTE_NAMES = ("api_thesis_defenses",)

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    *[item for item in patterns_for(thesis_patterns, ("api/v1/theses",)) if getattr(item, "name", None) not in COMMITTEE_ROUTE_NAMES],
]
