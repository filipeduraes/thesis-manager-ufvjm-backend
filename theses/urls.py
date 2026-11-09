"""
URL routes for the thesis submission and review API.
"""

from django.urls import path

from theses import views

urlpatterns = [
    path("api/v1/theses/me", views.StudentThesisView.as_view(), name="api_student_thesis"),
    path(
        "api/v1/theses/me/files",
        views.StudentThesisFileUploadView.as_view(),
        name="api_student_thesis_file_upload",
    ),
    path(
        "api/v1/theses/me/submit",
        views.StudentThesisSubmitView.as_view(),
        name="api_student_thesis_submit",
    ),
    path(
        "api/v1/theses/me/withdraw",
        views.StudentThesisWithdrawView.as_view(),
        name="api_student_thesis_withdraw",
    ),
    path("api/v1/theses", views.ThesisListView.as_view(), name="api_theses"),
    path(
        "api/v1/theses/<uuid:thesis_id>",
        views.ThesisDetailView.as_view(),
        name="api_thesis_detail",
    ),
    path(
        "api/v1/theses/<uuid:thesis_id>/reviews",
        views.ThesisReviewView.as_view(),
        name="api_thesis_review",
    ),
    path(
        "api/v1/theses/<uuid:thesis_id>/defense-result",
        views.ThesisDefenseResultView.as_view(),
        name="api_thesis_defense_result",
    ),
    path(
        "api/v1/theses/<uuid:thesis_id>/publish",
        views.ThesisPublishView.as_view(),
        name="api_thesis_publish",
    ),
    path(
        "api/v1/theses/<uuid:thesis_id>/files/<uuid:file_id>/download",
        views.ThesisFileDownloadView.as_view(),
        name="api_thesis_file_download",
    ),
    path(
        "api/v1/theses/<uuid:thesis_id>/documents",
        views.ThesisDocumentUploadView.as_view(),
        name="api_thesis_document_upload",
    ),
    path(
        "api/v1/theses/<uuid:thesis_id>/documents/<uuid:document_id>/validate",
        views.ThesisDocumentValidateView.as_view(),
        name="api_thesis_document_validate",
    ),
    path(
        "api/v1/theses/<uuid:thesis_id>/documents/<uuid:document_id>/download",
        views.ThesisDocumentDownloadView.as_view(),
        name="api_thesis_document_download",
    ),
    path(
        "api/v1/public/theses",
        views.PublicThesisListView.as_view(),
        name="api_public_theses",
    ),
    path(
        "api/v1/public/theses/<uuid:thesis_id>",
        views.PublicThesisDetailView.as_view(),
        name="api_public_thesis_detail",
    ),
    path(
        "api/v1/public/theses/<uuid:thesis_id>/download",
        views.PublicThesisDownloadView.as_view(),
        name="api_public_thesis_download",
    ),
]
