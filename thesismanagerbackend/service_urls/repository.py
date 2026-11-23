"""Anonymous HTTP API of the public thesis repository and search service."""

from django.urls import path

from accounts.urls import urlpatterns as account_patterns
from accounts.views import HealthView
from theses.urls import urlpatterns as thesis_patterns
from thesismanagerbackend.service_urls import patterns_for

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    *patterns_for(thesis_patterns, ("api/v1/public/theses", "api/v1/public/courses")),
    *patterns_for(account_patterns, ("api/v1/public/advisors",)),
]
