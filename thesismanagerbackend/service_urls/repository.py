"""Anonymous HTTP API of the public thesis repository and search service."""

from django.urls import path

from accounts.urls import urlpatterns as account_patterns
from accounts.views import HealthView
from repository_readmodel.views import (
    PublicCoursesReadModelView,
    PublicFiltersReadModelView,
    PublicThesisDetailReadModelView,
    PublicThesisListReadModelView,
)
from thesismanagerbackend.service_urls import patterns_for

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    path("api/v1/public/theses/filters", PublicFiltersReadModelView.as_view(), name="api_public_thesis_filters"),
    path("api/v1/public/courses", PublicCoursesReadModelView.as_view(), name="api_public_courses"),
    path("api/v1/public/theses/<uuid:thesis_id>", PublicThesisDetailReadModelView.as_view(), name="api_public_thesis_detail"),
    path("api/v1/public/theses", PublicThesisListReadModelView.as_view(), name="api_public_theses"),
    *patterns_for(account_patterns, ("api/v1/public/advisors",)),
]
