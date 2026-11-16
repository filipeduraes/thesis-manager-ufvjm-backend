"""
URL patterns for the accounts application.
"""
from django.urls import path

from accounts import views
from accounts import directory as accounts_directory

urlpatterns = [
    path("api/v1/users/suggest", views.UserSuggestView.as_view(), name="api_user_suggest"),
    path("api/v1/users", accounts_directory.UserDirectoryListView.as_view(), name="api_user_directory"),
    path("api/v1/public/advisors", accounts_directory.PublicAdvisorsListView.as_view(), name="api_public_advisors"),
    path("api/v1/session", views.SessionView.as_view(), name="api_session"),
    path("api/v1/auth/google/start", views.GoogleStartView.as_view(), name="api_google_start"),
    path(
        "api/v1/auth/external-token",
        views.ExternalTokenExchangeView.as_view(),
        name="api_external_token",
    ),
    path("api/v1/auth/dev-login", views.DevLoginView.as_view(), name="api_dev_login"),
    path("api/v1/auth/dev-users", views.DevUsersView.as_view(), name="api_dev_users"),
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
        "api/v1/admin/audit-logs/export",
        views.AdminAuditLogsExportView.as_view(),
        name="api_admin_audit_logs_export",
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
