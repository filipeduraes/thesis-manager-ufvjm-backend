"""
URL patterns for the accounts application.
"""
from django.urls import path

from accounts import views

urlpatterns = [
    path("api/v1/session", views.SessionView.as_view(), name="api_session"),
    path("api/v1/auth/google/start", views.GoogleStartView.as_view(), name="api_google_start"),
    path(
        "api/v1/auth/external-token",
        views.ExternalTokenExchangeView.as_view(),
        name="api_external_token",
    ),
    path("api/v1/admin/users", views.AdminUsersView.as_view(), name="api_admin_users"),
    path(
        "api/v1/admin/users/<uuid:user_id>",
        views.AdminUserDetailView.as_view(),
        name="api_admin_user_detail",
    ),
    path(
        "api/v1/admin/audit-logs",
        views.AdminAuditLogsView.as_view(),
        name="api_admin_audit_logs",
    ),
    path(
        "api/v1/admin/external-invitations",
        views.AdminExternalInvitationsView.as_view(),
        name="api_admin_external_invitations",
    ),
    path(
        "api/v1/admin/external-invitations/<uuid:invitation_id>",
        views.AdminExternalInvitationDetailView.as_view(),
        name="api_admin_external_invitation_detail",
    ),
]
