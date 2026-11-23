"""HTTP API of the defense committee and evaluation service."""

from django.urls import path

from accounts.views import HealthView
from theses.urls import urlpatterns as thesis_patterns
from thesismanagerbackend.service_urls import patterns_for

COMMITTEE_ROUTE_NAMES = (
    "api_thesis_defenses",
    "api_defenses",
    "api_defense_schedulable_theses",
    "api_defense_detail",
    "api_defense_cancel",
    "api_defense_start",
    "api_defense_close",
    "api_defense_members",
    "api_defense_external_member",
    "api_defense_member",
    "api_defense_member_regenerate_token",
    "api_defense_member_availability",
    "api_defense_evaluation",
    "api_defense_revert_status",
)

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    *patterns_for(thesis_patterns, ("api/v1/defenses",), names=COMMITTEE_ROUTE_NAMES),
]
