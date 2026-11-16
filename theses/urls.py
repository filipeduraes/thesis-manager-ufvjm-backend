"""
URL routes for the thesis submission and review API.
"""

from django.urls import path

from theses import views
from theses.views import (
    DefenseCancelView,
    DefenseCloseView,
    DefenseDetailView,
    DefenseEvaluationView,
    DefenseExternalMemberCreateView,
    DefenseExternalMemberTokenView,
    DefenseListCreateView,
    DefenseListView,
    DefenseMemberAvailabilityView,
    DefenseMemberCreateView,
    DefenseMemberDeleteView,
    DefenseRevertStatusView,
    DefenseStartView,
    SchedulableThesisListView,
)

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
    # TODO(auditoria): repositório público ainda sem `GET /api/v1/public/theses/filters`
    # (opções de curso/orientador/ano/palavra-chave presentes em publicados, cacheadas),
    # e sem cache para a busca anônima. Ver FUNCIONALIDADES.md
    # "Pendências identificadas na auditoria".
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
    # TODO(auditoria): backend ainda não oferece relatórios. Contrato proposto
    # na auditoria (2026-11-16): `GET /api/v1/reports/overview?courseId=&advisorId=&year=`
    # com `summary` (total; aprovadas = APPROVED/APPROVED_WITH_REMARKS/PUBLISHED;
    # reprovadas; pendentes = total - aprovadas - reprovadas; taxa de aprovação =
    # aprovadas / (aprovadas + reprovadas); média de dias do envio até a primeira
    # banca COMPLETED; contagem de bancas registradas/encerradas), `byCourse`, `byAdvisor`,
    # `byYear`, `options` (courses/advisors/years) e `filters` (somente dados dentro
    # do escopo do usuário: admin vê tudo, coordenador os cursos que coordena;
    # orientador e aluno recebem 403). Ver FUNCIONALIDADES.md.
    # Defense endpoints
    path("api/v1/theses/<uuid:thesis_id>/defenses", DefenseListCreateView.as_view(), name="api_thesis_defenses"),
    path("api/v1/defenses", DefenseListView.as_view(), name="api_defenses"),
    path("api/v1/defenses/schedulable-theses", SchedulableThesisListView.as_view(), name="api_defense_schedulable_theses"),
    path("api/v1/defenses/<uuid:defense_id>", DefenseDetailView.as_view(), name="api_defense_detail"),
    path("api/v1/defenses/<uuid:defense_id>/cancel", DefenseCancelView.as_view(), name="api_defense_cancel"),
    path("api/v1/defenses/<uuid:defense_id>/start", DefenseStartView.as_view(), name="api_defense_start"),
    path("api/v1/defenses/<uuid:defense_id>/close", DefenseCloseView.as_view(), name="api_defense_close"),
    path("api/v1/defenses/<uuid:defense_id>/members", DefenseMemberCreateView.as_view(), name="api_defense_members"),
    path("api/v1/defenses/<uuid:defense_id>/members/external", DefenseExternalMemberCreateView.as_view(), name="api_defense_external_member"),
    path("api/v1/defenses/<uuid:defense_id>/members/<uuid:member_id>", DefenseMemberDeleteView.as_view(), name="api_defense_member"),
    path("api/v1/defenses/<uuid:defense_id>/members/<uuid:member_id>/regenerate-token", DefenseExternalMemberTokenView.as_view(), name="api_defense_member_regenerate_token"),
    path("api/v1/defenses/<uuid:defense_id>/members/<uuid:member_id>/availability", DefenseMemberAvailabilityView.as_view(), name="api_defense_member_availability"),
    path("api/v1/defenses/<uuid:defense_id>/members/<uuid:member_id>/evaluation", DefenseEvaluationView.as_view(), name="api_defense_evaluation"),
    path("api/v1/defenses/<uuid:defense_id>/revert-status", DefenseRevertStatusView.as_view(), name="api_defense_revert_status"),
]
