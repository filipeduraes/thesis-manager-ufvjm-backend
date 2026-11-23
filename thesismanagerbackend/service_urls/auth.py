"""HTTP API of the authentication and user-management service."""

from django.urls import include, path

from accounts.views import HealthView
from accounts.urls import urlpatterns as account_patterns
from academic.urls import urlpatterns as academic_patterns
from thesismanagerbackend.service_urls import patterns_for

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    path("accounts/", include("allauth.urls")),
    *patterns_for(account_patterns, ("api/v1/session", "api/v1/auth/", "api/v1/users", "api/v1/public/advisors", "api/v1/admin/users", "api/v1/admin/audit-logs", "api/v1/admin/external-invitations")),
    *academic_patterns,
]
