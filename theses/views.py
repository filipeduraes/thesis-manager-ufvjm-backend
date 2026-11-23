"""
API views for the thesis submission, review and repository workflow.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.db.models import Q, QuerySet
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.renderers import BaseRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.audit_identifiers import EntityType
from accounts.audit_entry import AuditEntry
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditAction
from accounts.permissions import IsInstitutionalAdmin, IsInstitutionalUser
from accounts.services import google_social_accounts_prefetch,  record_audit
from academic.services import active_course_assignment
from theses.filters import parse_year_filter
from thesismanagerbackend.query_params import TRUTHY_QUERY_VALUES
from theses.models import (
    REVIEWABLE_STATUSES,
    StoredFile,
    Thesis,
    ThesisDocument,
    ThesisFile,
    ThesisOrigin,
    ThesisStatus,
)
from theses.permissions import IsAcademicManager, IsAdvisorUser, IsStudentUser
from theses.repository import (
    public_courses_payload,
    public_repository_filters_payload,
    public_thesis_detail_payload,
    public_thesis_list_payload,
    public_theses_queryset,
    published_theses,
    search_public_theses,
)
from theses.repository_cache import cached_payload, courses_cache_key, detail_cache_key, filters_cache_key, search_cache_key
from theses.repository_management import current_metadata, delete_thesis, import_legacy_thesis, replace_published_file, unpublish_thesis, update_published_thesis
from theses.serializers import (
    DefenseResultSerializer,
    DocumentUploadSerializer,
    LegacyThesisSerializer,
    PublishedFileSerializer,
    PublishedThesisUpdateSerializer,
    PublicThesisSearchSerializer,
    UnpublishThesisSerializer,
    ThesisMetadataSerializer,
    ThesisReviewSerializer,
)
from theses.services import (
    available_advisors,
    available_courses_for_student,
    coordinated_courses_filter,
    course_brief,
    document_payload,
    get_accessible_thesis,
    publish_thesis,
    record_defense_result,
    review_payload,
    review_thesis,
    save_thesis_metadata,
    submit_thesis,
    thesis_file_payload,
    thesis_list_queryset,
    withdraw_submission,
    thesis_list_payload,
    thesis_payload,
    theses_visible_to,
    upload_thesis_document,
    upload_thesis_file,
    user_brief,
    user_can_access_thesis,
    user_is_admin,
    user_roles,
    validate_thesis_document,
    verify_stored_file_integrity,
)

logger = logging.getLogger(__name__)


class BinaryFileRenderer(BaseRenderer):
    """
    Permite respostas binárias/arquivo em views DRF sem negociação JSON.
    """

    media_type = "*/*"
    format = "bin"
    charset = None
    render_style = "binary"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        """
        Retorna o conteúdo sem serialização.

        Args:
            data: Conteúdo da resposta.
            accepted_media_type: Tipo aceito pela negociação.
            renderer_context: Contexto do renderer.

        Returns:
            object: Conteúdo original.
        """
        return data


def _error_detail(exc: DjangoValidationError) -> tuple[str, Any]:
    """
    Build a consistent error detail from a Django validation error.

    Args:
        exc: Django validation error.

    Returns:
        tuple[str, Any]: Human-readable message and structured errors.
    """
    if hasattr(exc, "message_dict"):
        errors: Any = exc.message_dict
        first_message = next(iter(errors.values())) if errors else "Dados inválidos."
        if isinstance(first_message, list) and first_message:
            first_message = first_message[0]
        return str(first_message), errors
    messages = list(exc.messages)
    return (messages[0] if messages else "Dados inválidos.", messages)


def _validation_response(exc: DjangoValidationError) -> Response:
    """
    Convert a Django validation error into an HTTP 400 response.

    Args:
        exc: Django validation error.

    Returns:
        Response: API error response.
    """
    detail, errors = _error_detail(exc)
    return Response({"detail": detail, "errors": errors}, status=status.HTTP_400_BAD_REQUEST)


def visible_thesis_or_404(user: User, thesis_id: str) -> Thesis:
    """
    Return a thesis visible to ``user`` or raise 404.

    Args:
        user: Authenticated user requesting the thesis.
        thesis_id: Identifier of the requested thesis.

    Returns:
        Thesis: The visible thesis.

    Raises:
        Http404: When the thesis does not exist or is not visible to the user.
    """
    thesis = theses_visible_to(user).filter(pk=thesis_id).first()
    if thesis is None:
        is_committee_member = DefenseMember.objects.filter(defense__thesis_id=thesis_id, institutional_member=user).exists()
        if is_committee_member:
            thesis = Thesis.objects.filter(pk=thesis_id, deleted_at__isnull=True).first()
    if thesis is None:
        raise Http404("Monografia não encontrada.")
    return thesis


class ThesisPagination(PageNumberPagination):
    """
    Pagination with frontend-friendly metadata for thesis lists.
    """

    page_size = 10
    page_size_query_param = "pageSize"
    max_page_size = 100

    def get_paginated_response(self, data: list[dict[str, Any]]) -> Response:
        """
        Return paginated results with explicit metadata.

        Args:
            data: Serialized results for the current page.

        Returns:
            Response: Paginated payload.
        """
        page_size = self.get_page_size(self.request) or self.page_size
        count = self.page.paginator.count
        total_pages = self.page.paginator.num_pages
        page = self.page.number
        last_page_count = count % page_size
        if last_page_count == 0 and count > 0:
            last_page_count = page_size
        return Response(
            {
                "count": count,
                "page": page,
                "pageSize": page_size,
                "totalPages": total_pages,
                "lastPageCount": last_page_count,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "results": data,
            }
        )


def _scoped_theses(request, base_queryset: QuerySet[Thesis] | None = None) -> QuerySet[Thesis]:
    """
    Apply role, scope and filter parameters to the visible thesis queryset.

    Args:
        request: HTTP request.
        base_queryset: Optional queryset with list relations prefetched.

    Returns:
        QuerySet[Thesis]: Filtered queryset.
    """
    base = base_queryset or thesis_list_queryset()
    visible = theses_visible_to(request.user, base_queryset=base)
    committee_theses = Thesis.objects.filter(defenses__members__institutional_member=request.user).values("pk")
    queryset = base.filter(Q(pk__in=visible.order_by().values("pk")) | Q(pk__in=committee_theses)).distinct()
    scope = (request.query_params.get("scope") or "").strip().lower()
    if scope == "advised":
        queryset = queryset.filter(advisor_id=request.user.pk)
    elif scope == "authored":
        queryset = queryset.filter(authors__author_id=request.user.pk)
    elif scope == "coordinated":
        coordinated_filter = coordinated_courses_filter(request.user, prefix="course__")
        queryset = queryset.filter(coordinated_filter)
    query = (request.query_params.get("q") or "").strip()
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(abstract__icontains=query)
            | Q(reference_code__icontains=query)
            | Q(authors__author__name__icontains=query)
            | Q(advisor__name__icontains=query)
            | Q(keyword_links__keyword__name__icontains=query)
        )
    status_filter = (request.query_params.get("status") or "").strip()
    if status_filter:
        statuses = [value.strip().upper() for value in status_filter.split(",") if value.strip()]
        valid_statuses = [value for value in statuses if value in ThesisStatus.values]
        if valid_statuses:
            queryset = queryset.filter(status__in=valid_statuses)
    course_id = (request.query_params.get("courseId") or "").strip()
    if course_id:
        queryset = queryset.filter(course_id=course_id)
    advisor_id = (request.query_params.get("advisorId") or "").strip()
    if advisor_id:
        queryset = queryset.filter(advisor_id=advisor_id)
    year = (request.query_params.get("year") or "").strip()
    if year:
        parsed_year = parse_year_filter(year)
        if parsed_year is not None:
            queryset = queryset.filter(Q(submitted_at__year=parsed_year) | Q(published_at__year=parsed_year))
    pending_review = (request.query_params.get("pendingReview") or "").strip().lower()
    if pending_review in TRUTHY_QUERY_VALUES:
        queryset = queryset.filter(status__in=REVIEWABLE_STATUSES)
    return queryset.distinct().order_by("-updated_at")


def _student_thesis(user) -> Thesis | None:
    """
    Return the thesis authored by the student.

    Args:
        user: Student user.

    Returns:
        Thesis | None: Main-authored thesis when present.
    """
    return (
        Thesis.objects.select_related("course", "advisor")
        .filter(origin=ThesisOrigin.WORKFLOW, authors__author=user)
        .order_by("-created_at")
        .first()
    )


def _download_response(stored_file: StoredFile, thesis: Thesis, disposition: str, version_number: int | None = None) -> FileResponse:
    """
    Build a safe file response for a stored thesis file.

    Args:
        stored_file: Stored file to stream.
        thesis: Thesis that owns the file.
        disposition: ``inline`` or ``attachment``.
        version_number: Optional version number used in the download name.

    Returns:
        FileResponse: Streamed PDF response.

    Raises:
        Http404: When the physical file is missing.
    """
    if not stored_file.storage_path:
        raise Http404("Arquivo não encontrado.")
    try:
        handle = default_storage.open(stored_file.storage_path, "rb")
    except FileNotFoundError as exc:
        logger.error("Stored file %s is missing from storage", stored_file.pk)
        raise Http404("Arquivo não encontrado.") from exc
    safe_reference = "".join(character for character in thesis.reference_code if character.isalnum() or character in {"-", "_"})
    suffix = f"-v{version_number}" if version_number else ""
    filename = f"{safe_reference or 'monografia'}{suffix}.pdf"
    as_attachment = disposition != "inline"
    response = FileResponse(handle, as_attachment=as_attachment, filename=filename, content_type="application/pdf")
    response["X-Content-Type-Options"] = "nosniff"
    if not thesis.is_public:
        response["Cache-Control"] = "private, no-store"
    return response


class StudentThesisView(APIView):
    """
    Read and update the thesis owned by the authenticated student.
    """

    permission_classes = [IsInstitutionalUser, IsStudentUser]
    throttle_scope = "thesis"

    def get(self, request) -> Response:
        """
        Return the student thesis, form options and upload limits.

        Args:
            request: HTTP request.

        Returns:
            Response: Thesis context payload.
        """
        thesis = _student_thesis(request.user)
        courses = available_courses_for_student(request.user)
        if thesis is not None and all(course.pk != thesis.course_id for course in courses):
            courses = [thesis.course, *courses]
        advisors = list(available_advisors())
        if thesis is not None and all(advisor.pk != thesis.advisor_id for advisor in advisors):
            advisors = [thesis.advisor, *advisors]
        assignment = active_course_assignment(request.user)
        payload = {
            "thesis": thesis_payload(thesis, request.user) if thesis else None,
            "courses": [course_brief(course) for course in courses],
            "advisors": [user_brief(advisor) for advisor in advisors],
            "defaultCourseId": str(assignment.course_id) if assignment else None,
            "limits": {
                "maxPdfSizeMb": getattr(settings, "THESIS_MAX_PDF_SIZE_MB", 50),
                "maxDocumentSizeMb": getattr(settings, "THESIS_MAX_DOCUMENT_SIZE_MB", 20),
                "maxKeywords": getattr(settings, "THESIS_MAX_KEYWORDS", 10),
                "maxAuthors": getattr(settings, "THESIS_MAX_AUTHORS", 5),
            },
        }
        return Response(payload)

    def put(self, request) -> Response:
        """
        Create or update the student thesis metadata as a draft.

        Args:
            request: HTTP request.

        Returns:
            Response: Updated thesis payload.
        """
        serializer = ThesisMetadataSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            thesis = save_thesis_metadata(
                request.user,
                data["course"],
                data["advisor"],
                data["title"],
                data["abstract"],
                data["keywords"],
                thesis_type=data.get("type", "MONOGRAPH"),
                journal=data.get("journal", ""),
                author_ids=data.get("authors", []),
                request=request,
            )
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})


class StudentThesisFileUploadView(APIView):
    """
    Upload a new PDF version for the authenticated student's thesis.
    """

    permission_classes = [IsInstitutionalUser, IsStudentUser]
    throttle_scope = "thesis_upload"

    def post(self, request) -> Response:
        """
        Store a PDF version and return the updated thesis.

        Args:
            request: HTTP request with multipart ``file``.

        Returns:
            Response: Uploaded file and thesis payload.
        """
        thesis = _student_thesis(request.user)
        if thesis is None:
            return Response(
                {"detail": "Crie o rascunho da monografia antes de enviar o arquivo."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        uploaded_file = request.FILES.get("file")
        try:
            thesis_file = upload_thesis_file(
                request.user,
                thesis,
                uploaded_file,
                request.data.get("changeDescription", ""),
                request,
            )
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response(
            {
                "file": thesis_file_payload(thesis_file),
                "thesis": thesis_payload(thesis, request.user),
            },
            status=status.HTTP_201_CREATED,
        )


class StudentThesisSubmitView(APIView):
    """
    Submit the current thesis version for advisor review.
    """

    permission_classes = [IsInstitutionalUser, IsStudentUser]
    throttle_scope = "thesis"

    def post(self, request) -> Response:
        """
        Submit the student thesis.

        Args:
            request: HTTP request.

        Returns:
            Response: Updated thesis payload.
        """
        thesis = _student_thesis(request.user)
        if thesis is None:
            return Response({"detail": "Nenhuma monografia encontrada."}, status=status.HTTP_404_NOT_FOUND)
        try:
            thesis = submit_thesis(request.user, thesis, request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})


class ThesisListView(APIView):
    """
    List theses visible to the authenticated user.
    """

    permission_classes = [IsInstitutionalUser]
    throttle_scope = "thesis"

    def get(self, request) -> Response:
        """
        List role-scoped theses with search and filters.

        Args:
            request: HTTP request.

        Returns:
            Response: Paginated thesis summaries.
        """
        queryset = _scoped_theses(request).select_related("course", "advisor")
        paginator = ThesisPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        viewer_context = {
            "roles": user_roles(request.user),
            "is_admin": user_is_admin(request.user),
            "accessible_ids": {thesis.pk for thesis in page or []},
        }
        results = [thesis_list_payload(thesis, request.user, viewer_context) for thesis in page]
        return paginator.get_paginated_response(results)


class ThesisDetailView(APIView):
    """
    Read a thesis visible to the authenticated user.
    """

    permission_classes = [IsInstitutionalUser]
    throttle_scope = "thesis"

    def get(self, request, thesis_id) -> Response:
        """
        Return a thesis with its full version history.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: Thesis payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})

    def delete(self, request, thesis_id) -> Response:
        """
        Soft-delete a thesis that was never published.

        Published theses are refused with 400 (even for administrators); they can
        only be unpublished through ``POST /api/v1/theses/<id>/unpublish``.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: 204 on success, 400 when refused, 403 outside the manager's courses.
        """
        if not IsAcademicManager().has_permission(request, self):
            raise PermissionDenied(IsAcademicManager.message)
        thesis = visible_thesis_or_404(request.user, thesis_id)
        try:
            delete_thesis(request.user, thesis, request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LegacyThesisCreateView(APIView):
    """
    Register a legacy (already approved) thesis directly in the public repository.
    """

    permission_classes = [IsInstitutionalUser, IsAcademicManager]
    throttle_scope = "thesis_upload"

    def post(self, request) -> Response:
        """
        Create a published thesis with origin ``LEGACY_IMPORT`` from multipart data.

        Args:
            request: HTTP request (multipart) with ``title``, ``abstract``, ``keywords``,
                ``authors`` (JSON list of ``{name, userId}``), ``advisorId``, ``courseId``,
                ``defenseYear`` and/or ``defenseDate``, ``result``, optional ``type`` and
                ``journal``, and the PDF in ``file``.

        Returns:
            Response: 201 with the thesis payload; 400 for invalid data; 403 outside
                the coordinator's courses.
        """
        serializer = LegacyThesisSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            thesis = import_legacy_thesis(request.user, serializer.to_metadata(), serializer.validated_data["result"], serializer.validated_data["file"], request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)}, status=status.HTTP_201_CREATED)


class PublishedThesisUpdateView(APIView):
    """
    Edit the metadata of a published thesis.
    """

    permission_classes = [IsInstitutionalUser, IsAcademicManager]
    throttle_scope = "thesis"

    def patch(self, request, thesis_id) -> Response:
        """
        Apply a partial edit to a published thesis and record it.

        Args:
            request: HTTP request (JSON) with any of ``title``, ``abstract``, ``keywords``,
                ``authors``, ``advisorId``, ``courseId``, ``defenseYear``, ``defenseDate``,
                ``type``, ``journal`` and an optional ``reason``.
            thesis_id: Thesis identifier.

        Returns:
            Response: Updated thesis payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        serializer = PublishedThesisUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        metadata = serializer.merge_into(current_metadata(thesis))
        try:
            thesis = update_published_thesis(request.user, thesis, metadata, serializer.validated_data["reason"], request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})


class PublishedThesisFileView(APIView):
    """
    Replace the PDF of a published thesis with a new version.
    """

    permission_classes = [IsInstitutionalUser, IsAcademicManager]
    throttle_scope = "thesis_upload"

    def post(self, request, thesis_id) -> Response:
        """
        Upload a new PDF version; the previous one stays in the history.

        Args:
            request: HTTP request (multipart) with ``file`` and optional ``changeDescription``.
            thesis_id: Thesis identifier.

        Returns:
            Response: 201 with the new ``file`` and the thesis payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        serializer = PublishedFileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            thesis_file = replace_published_file(request.user, thesis, serializer.validated_data["file"], serializer.validated_data["change_description"], request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"file": thesis_file_payload(thesis_file), "thesis": thesis_payload(thesis, request.user)}, status=status.HTTP_201_CREATED)


class ThesisUnpublishView(APIView):
    """
    Remove a published thesis from the public repository.
    """

    permission_classes = [IsInstitutionalUser, IsAcademicManager]
    throttle_scope = "thesis"

    def post(self, request, thesis_id) -> Response:
        """
        Unpublish a thesis with a mandatory reason.

        Args:
            request: HTTP request with ``reason``.
            thesis_id: Thesis identifier.

        Returns:
            Response: Updated thesis payload (status ``ARCHIVED``).
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        serializer = UnpublishThesisSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            thesis = unpublish_thesis(request.user, thesis, serializer.validated_data["reason"], request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})


class ThesisReviewView(APIView):
    """
    Record an advisor decision for a submitted thesis.
    """

    permission_classes = [IsInstitutionalUser]
    throttle_scope = "thesis"

    def post(self, request, thesis_id) -> Response:
        """
        Register an approval, change request or rejection.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: Updated thesis and review payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        serializer = ThesisReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            review = review_thesis(
                request.user,
                thesis,
                data["decision"],
                data["comments"],
                data["recommendations"],
                request,
            )
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user), "review": review_payload(review)})


class ThesisDefenseResultView(APIView):
    """
    Record the final defense result for an advisor-approved thesis.
    """

    permission_classes = [IsInstitutionalUser, IsAcademicManager]
    throttle_scope = "thesis"

    def post(self, request, thesis_id) -> Response:
        """
        Record a final defense result.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: Updated thesis payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        serializer = DefenseResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            thesis = record_defense_result(request.user, thesis, data["result"], data["reason"], request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})


class StudentThesisWithdrawView(APIView):
    """
    Descarta o envio atual da monografia antes da avaliação do orientador.
    """

    permission_classes = [IsInstitutionalUser, IsStudentUser]
    throttle_scope = "thesis"

    def post(self, request) -> Response:
        """
        Reverte um envio ainda não avaliado para estado editável.

        Args:
            request: Requisição HTTP.

        Returns:
            Response: Monografia atualizada.
        """
        thesis = _student_thesis(request.user)
        if thesis is None:
            return Response({"detail": "Nenhuma monografia encontrada."}, status=status.HTTP_404_NOT_FOUND)
        try:
            thesis = withdraw_submission(request.user, thesis, request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})


class ThesisPublishView(APIView):
    """
    Publish an approved thesis in the public repository.
    """

    permission_classes = [IsInstitutionalUser, IsAcademicManager]
    throttle_scope = "thesis"

    def post(self, request, thesis_id) -> Response:
        """
        Publish a thesis after eligibility checks.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: Updated thesis payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        try:
            thesis = publish_thesis(request.user, thesis, request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        thesis = get_accessible_thesis(request.user, str(thesis.pk))
        return Response({"thesis": thesis_payload(thesis, request.user)})


class ThesisFileDownloadView(APIView):
    """
    Stream a thesis version after permission checks.
    """

    permission_classes = [IsInstitutionalUser]
    renderer_classes = [BinaryFileRenderer]
    throttle_scope = "thesis"

    def get(self, request, thesis_id, file_id) -> FileResponse:
        """
        Download or preview a thesis PDF.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.
            file_id: Thesis file identifier.

        Returns:
            FileResponse: Streamed PDF.
        """
        thesis = Thesis.objects.filter(pk=thesis_id, deleted_at__isnull=True).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
        if not user_can_access_thesis(request.user, thesis):
            raise Http404("Monografia não encontrada.")
        thesis_file = ThesisFile.objects.select_related("stored_file").filter(pk=file_id, thesis=thesis).first()
        if thesis_file is None:
            raise Http404("Arquivo não encontrado.")
        disposition = (request.query_params.get("disposition") or "attachment").strip().lower()
        if disposition not in {"inline", "attachment"}:
            disposition = "attachment"
        record_audit(AuditEntry(
            action=AuditAction.DOWNLOAD,
            entity_type=EntityType.THESIS_FILE,
            entity_id=str(thesis_file.pk),
            request=request,
            metadata={
                "thesisId": str(thesis.pk),
                "versionNumber": thesis_file.version_number,
                "disposition": disposition,
                "fileName": thesis_file.stored_file.original_filename,
            },
        ))
        return _download_response(thesis_file.stored_file, thesis, disposition, thesis_file.version_number)


class ThesisDocumentUploadView(APIView):
    """
    Upload a supplementary document for a thesis.
    """

    permission_classes = [IsInstitutionalUser]
    throttle_scope = "thesis_upload"

    def post(self, request, thesis_id) -> Response:
        """
        Store a supplementary PDF document.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: Document payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        serializer = DocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            document = upload_thesis_document(request.user, thesis, data["type"], data["file"], request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"document": document_payload(document)}, status=status.HTTP_201_CREATED)


class ThesisDocumentValidateView(APIView):
    """
    Validate a supplementary thesis document.
    """

    permission_classes = [IsInstitutionalUser, IsAcademicManager]
    throttle_scope = "thesis"

    def post(self, request, thesis_id, document_id) -> Response:
        """
        Mark a document as validated.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.
            document_id: Document identifier.

        Returns:
            Response: Document payload.
        """
        thesis = visible_thesis_or_404(request.user, thesis_id)
        document = ThesisDocument.objects.filter(pk=document_id, thesis=thesis).first()
        if document is None:
            raise Http404("Documento não encontrado.")
        try:
            document = validate_thesis_document(request.user, thesis, document, request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"document": document_payload(document)})


class ThesisDocumentDownloadView(APIView):
    """
    Stream a supplementary thesis document after permission checks.
    """

    permission_classes = [IsInstitutionalUser]
    renderer_classes = [BinaryFileRenderer]
    throttle_scope = "thesis"

    def get(self, request, thesis_id, document_id) -> FileResponse:
        """
        Download a supplementary document.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.
            document_id: Document identifier.

        Returns:
            FileResponse: Streamed PDF.
        """
        thesis = Thesis.objects.filter(pk=thesis_id, deleted_at__isnull=True).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
        if not user_can_access_thesis(request.user, thesis):
            raise Http404("Monografia não encontrada.")
        document = ThesisDocument.objects.select_related("stored_file").filter(pk=document_id, thesis=thesis).first()
        if document is None:
            raise Http404("Documento não encontrado.")
        record_audit(AuditEntry(
            action=AuditAction.DOWNLOAD,
            entity_type=EntityType.THESIS_DOCUMENT,
            entity_id=str(document.pk),
            request=request,
            metadata={
                "thesisId": str(thesis.pk),
                "documentType": document.type,
                "fileName": document.stored_file.original_filename,
            },
        ))
        return _download_response(document.stored_file, thesis, "attachment")


def _cached_response(payload: dict[str, Any], from_cache: bool) -> Response:
    """
    Wrap a public repository payload, flagging whether it came from the cache.

    Args:
        payload: Response body.
        from_cache: Whether the body was read from Redis.

    Returns:
        Response: JSON response with ``X-Repository-Cache: HIT|MISS``.
    """
    response = Response(payload)
    response["X-Repository-Cache"] = "HIT" if from_cache else "MISS"
    return response


class PublicRepositoryView(APIView):
    """
    Base class of the anonymous repository endpoints.

    No authentication runs (session cookies are ignored), and every endpoint is
    rate limited per client IP. The throttle uses ``accounts.network.get_client_ip``,
    so ``X-Forwarded-For`` is only honored from peers in ``TRUSTED_PROXY_CIDRS``.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "public_repository"


class PublicThesisListView(PublicRepositoryView):
    """
    Search published theses in the public repository.
    """

    def get(self, request) -> Response:
        """
        Return published theses matching the search parameters.

        Args:
            request: HTTP request. Query parameters: ``q`` (free text), ``title``,
                ``author``, ``advisor``, ``course`` (text), ``keyword`` (repeatable,
                exact), ``courseId``, ``advisorId``, ``year`` (defense year),
                ``ordering`` (``relevance``, ``recent``, ``title``), ``page`` and ``pageSize``.

        Returns:
            Response: Paginated public summaries; 400 for invalid parameters.
        """
        serializer = PublicThesisSearchSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filters = serializer.to_filters()
        page_param = str(request.query_params.get("page") or "1")
        page_size_param = str(request.query_params.get("pageSize") or "")
        cache_key = search_cache_key(filters, page_param, page_size_param)

        def build_page() -> dict[str, Any]:
            queryset = search_public_theses(filters)
            paginator = ThesisPagination()
            page = paginator.paginate_queryset(queryset, request, view=self)
            results = [public_thesis_list_payload(thesis) for thesis in page]
            page_data = dict(paginator.get_paginated_response(results).data)
            page_data["ordering"] = filters.effective_ordering
            return page_data

        payload, from_cache = cached_payload(cache_key, build_page)
        return _cached_response(payload, from_cache)


class PublicThesisFiltersView(PublicRepositoryView):
    """
    List the filter options present in published theses.
    """

    def get(self, request) -> Response:
        """
        Return courses, advisors, defense years and keywords of published theses.

        Args:
            request: HTTP request.

        Returns:
            Response: ``courses``, ``advisors``, ``years`` and ``keywords`` with counts.
        """
        payload, from_cache = cached_payload(filters_cache_key(), public_repository_filters_payload)
        return _cached_response(payload, from_cache)


class PublicCourseListView(PublicRepositoryView):
    """
    List the institution's courses for anonymous visitors.
    """

    def get(self, request) -> Response:
        """
        Return every course with its number of published theses.

        Args:
            request: HTTP request. ``withPublished=true`` keeps only courses with
                at least one published thesis.

        Returns:
            Response: ``count`` and ``results``.
        """
        only_with_published = (request.query_params.get("withPublished") or "").strip().lower() in TRUTHY_QUERY_VALUES

        def build_courses() -> dict[str, Any]:
            results = public_courses_payload(only_with_published=only_with_published)
            return {"count": len(results), "results": results}

        payload, from_cache = cached_payload(courses_cache_key(only_with_published), build_courses)
        return _cached_response(payload, from_cache)


class PublicThesisDetailView(PublicRepositoryView):
    """
    Return a published thesis from the public repository.
    """

    def get(self, request, thesis_id) -> Response:
        """
        Return public thesis metadata, including the SHA-256 of the published PDF.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: Public thesis payload.
        """

        def build_detail() -> dict[str, Any] | None:
            thesis = public_theses_queryset().filter(pk=thesis_id).first()
            if thesis is None:
                return None
            return {"thesis": public_thesis_detail_payload(thesis)}

        payload, from_cache = cached_payload(detail_cache_key(str(thesis_id)), build_detail)
        if payload is None:
            raise Http404("Monografia não encontrada.")
        return _cached_response(payload, from_cache)


class PublicThesisDownloadView(PublicRepositoryView):
    """
    Stream the current file of a published thesis.
    """

    throttle_scope = "public_download"

    def perform_content_negotiation(self, request, force: bool = False):
        """
        Negotiate leniently so errors (404, 429) are always JSON.

        The PDF itself is a ``FileResponse`` and bypasses the renderers.

        Args:
            request: Incoming request.
            force: Ignored; negotiation is always forced.

        Returns:
            tuple: Renderer and media type for error responses.
        """
        return super().perform_content_negotiation(request, force=True)

    def get(self, request, thesis_id) -> FileResponse:
        """
        Download or preview the current public PDF after rechecking its SHA-256.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            FileResponse: Streamed PDF.
        """
        thesis = published_theses().filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
        thesis_file = thesis.files.filter(is_current=True).select_related("stored_file").first()
        if thesis_file is None:
            raise Http404("Arquivo não encontrado.")
        if not verify_stored_file_integrity(thesis_file.stored_file):
            logger.error("Public thesis %s failed integrity verification", thesis.pk)
            raise Http404("Arquivo não encontrado.")
        disposition = (request.query_params.get("disposition") or "attachment").strip().lower()
        if disposition not in {"inline", "attachment"}:
            disposition = "attachment"
        return _download_response(thesis_file.stored_file, thesis, disposition, thesis_file.version_number)


# ─── Defense API helpers ──────────────────────────────────────────────────────

from theses.defense_committee import add_external_member, add_institutional_member, coordinator_managed_theses, defense_availability_summary, defense_member_payload, regenerate_external_member_token, remove_member, resolve_committee_member, respond_availability  # noqa: E402
from theses.defense_evaluation import defense_evaluation_payload, record_committee_evaluation  # noqa: E402
from theses.defense_listing import DEFENSE_LIST_PREFETCH, DefenseListFilters, defense_list_payload, defense_viewer_context_for, defenses_visible_to, list_visible_defenses, schedulable_theses_for, viewer_membership_payload  # noqa: E402
from theses.defense_scheduling import cancel_defense, defense_close_entries_prefetch, close_defense, defense_payload, defense_status_history_payload, revert_defense_status, schedule_defense, start_defense, sync_defense_status, update_defense  # noqa: E402
from theses.models import Defense, DefenseMember, DefenseStatus  # noqa: E402
from theses.permissions import IsDefenseCommitteeMember  # noqa: E402
from theses.serializers import AddExternalMemberSerializer, AddInstitutionalMemberSerializer, AvailabilityResponseSerializer, ScheduleDefenseSerializer, SubmitEvaluationSerializer, UpdateDefenseSerializer  # noqa: E402
from theses.services import user_is_admin, user_is_advisor, user_is_coordinator  # noqa: E402


def _scoped_defense_qs(user) -> QuerySet:
    """
    Return a Defense queryset restricted to what the given principal may access.

    Delegates to ``defenses_visible_to`` so that detail and listing endpoints share
    the same visibility rule.
    """
    return defenses_visible_to(user)


def _scoped_thesis(user, thesis_id) -> Thesis:
    """
    Return the Thesis accessible to the user, raising Http404 otherwise.

    - Admin: any thesis.
    - Coordinator: theses of the courses they coordinate (plus advised ones).
    - Advisor: only theses they advise.
    - Student: only their authored (main) thesis.
    """
    if user_is_admin(user):
        return get_object_or_404(Thesis.objects.filter(deleted_at__isnull=True), pk=thesis_id)
    if user_is_coordinator(user):
        managed_thesis_ids = coordinator_managed_theses(user).values("pk")
        allowed = Q(pk__in=managed_thesis_ids)
        if user_is_advisor(user):
            allowed |= Q(advisor_id=user.pk)
        return get_object_or_404(Thesis.objects.filter(allowed, deleted_at__isnull=True), pk=thesis_id)
    if user_is_advisor(user):
        return get_object_or_404(Thesis.objects.filter(deleted_at__isnull=True), pk=thesis_id, advisor_id=user.pk)
    return get_object_or_404(Thesis.objects.filter(deleted_at__isnull=True), pk=thesis_id, authors__author=user)


def _external_token_response(member: DefenseMember, access_token: str, viewer: Any, status_code: int) -> Response:
    """
    Build the one-time response that carries an external member's readable token.

    Args:
        member: External committee member with its current invitation.
        access_token: Readable token, shown only in this response.
        viewer: Authenticated user, used for member payload visibility.
        status_code: HTTP status code of the response.

    Returns:
        Response: Payload with ``member``, ``accessToken`` and ``accessTokenExpiresAt``, not cacheable.
    """
    expires_at = member.external_invitation.expires_at.isoformat()
    response = Response({"member": defense_member_payload(member, viewer=viewer), "accessToken": access_token, "accessTokenExpiresAt": expires_at}, status=status_code)
    # O token é exibido uma única vez: impede que proxies ou o navegador o guardem em cache.
    response["Cache-Control"] = "no-store"
    return response


# ─── Defense views ────────────────────────────────────────────────────────────


class DefenseListView(APIView):
    """
    List every defense the authenticated institutional user may see.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser]

    def get(self, request) -> Response:
        """
        List role-scoped defenses with optional search and filters.

        External invitation sessions are rejected by ``IsInstitutionalUser`` (403);
        they read their committee through ``GET /api/v1/defenses/<id>``.

        Args:
            request: HTTP request. Query parameters: ``q`` (free-text search),
                ``status`` (comma-separated DefenseStatus codes) and
                ``pendingEvaluation`` (``true``/``1``/``yes``).

        Returns:
            Response: Paginated defense list payloads.
        """
        filters = DefenseListFilters.from_query_params(request.query_params)
        queryset = list_visible_defenses(request.user, filters=filters, request=request)
        paginator = ThesisPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        viewer_context = defense_viewer_context_for(page, request.user)
        results = [defense_list_payload(defense, viewer=request.user, viewer_context=viewer_context) for defense in page]
        return paginator.get_paginated_response(results)


class SchedulableThesisListView(APIView):
    """
    List theses for which the authenticated user may schedule a defense.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser]

    def get(self, request) -> Response:
        """
        List advisor-approved theses without an active defense, scoped by ``_can_manage_defense``.

        Users without scheduling rights (e.g. students) receive an empty page.

        Args:
            request: HTTP request.

        Returns:
            Response: Paginated thesis summaries (same shape as ``GET /api/v1/theses``).
        """
        queryset = schedulable_theses_for(request.user)
        paginator = ThesisPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        results = [thesis_list_payload(thesis, request.user) for thesis in page]
        return paginator.get_paginated_response(results)


class DefenseListCreateView(APIView):
    """
    List defenses for a thesis or schedule a new one.
    """

    throttle_scope = "thesis"

    def get_permissions(self):
        """
        Return stricter permissions for POST (schedule requires advisor or manager role).

        Returns:
            list: Permission instances.
        """
        if self.request.method == "POST":
            return [(IsAcademicManager | IsAdvisorUser)()]
        return [IsInstitutionalUser()]

    def get(self, request, thesis_id) -> Response:
        """
        List all defenses for a thesis accessible to the authenticated user.

        Args:
            request: HTTP request.
            thesis_id: Thesis primary key from URL.

        Returns:
            Response: List of defense payloads.
        """
        thesis = _scoped_thesis(request.user, thesis_id)
        defenses = list(
            Defense.objects.filter(thesis=thesis)
            .select_related("thesis", "thesis__course", "thesis__advisor")
            .prefetch_related(
                *DEFENSE_LIST_PREFETCH,
                defense_close_entries_prefetch(),
                google_social_accounts_prefetch("members__institutional_member__socialaccount_set"),
            )
            .order_by("-scheduled_datetime")
        )
        viewer_context = defense_viewer_context_for(defenses, request.user)
        return Response({"defenses": [defense_payload(defense, viewer=request.user, viewer_context=viewer_context) for defense in defenses]})

    def post(self, request, thesis_id) -> Response:
        """
        Schedule a defense for the specified thesis.

        Args:
            request: HTTP request with scheduling data.
            thesis_id: Thesis primary key from URL.

        Returns:
            Response: Created defense payload (201).
        """
        thesis = _scoped_thesis(request.user, thesis_id)
        serializer = ScheduleDefenseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            defense = schedule_defense(thesis, actor=request.user, request=request, **serializer.validated_data)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"defense": defense_payload(defense, viewer=request.user)}, status=status.HTTP_201_CREATED)


class DefenseDetailView(APIView):
    """
    Retrieve or update a specific defense session.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser | IsDefenseCommitteeMember]

    def get(self, request, defense_id) -> Response:
        """
        Return the defense with its committee members, chronological ``statusHistory``,
        availability summary, the viewer's own ``viewerMembership`` and ``viewerPermissions``.

        Args:
            request: HTTP request.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Defense payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        viewer_context = defense_viewer_context_for([defense], request.user)
        detail_payload = defense_payload(defense, viewer=request.user, viewer_context=viewer_context)
        detail_payload["statusHistory"] = defense_status_history_payload(defense)
        viewer_permissions = viewer_context.permissions_for(defense)
        detail_payload.update(defense_availability_summary(defense, include_pending_confirmations=viewer_permissions.can_manage))
        detail_payload["viewerMembership"] = viewer_membership_payload(defense, request.user)
        detail_payload["viewerPermissions"] = viewer_permissions.to_payload()
        return Response({"defense": detail_payload})

    def patch(self, request, defense_id) -> Response:
        """
        Update scheduling details for a not-yet-started defense.

        Args:
            request: HTTP request with updated fields.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Updated defense payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        serializer = UpdateDefenseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            defense = update_defense(defense, actor=request.user, request=request, **serializer.validated_data)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"defense": defense_payload(defense, viewer=request.user)})


class DefenseCancelView(APIView):
    """
    Cancel a scheduled defense and restore the thesis status.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser]

    def post(self, request, defense_id) -> Response:
        """
        Cancel the defense.

        Args:
            request: HTTP request with optional ``reason`` field.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Updated defense payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        try:
            defense = cancel_defense(defense, reason=(request.data.get("reason") or ""), actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"defense": defense_payload(defense, viewer=request.user)})


class DefenseStartView(APIView):
    """
    Transition a defense from SCHEDULED to IN_PROGRESS.

    Admins, coordinators and the advisor of the thesis may call it; the object-level
    rule (advisor of this thesis) is enforced by the service via ``_can_manage_defense``.
    """

    throttle_scope = "thesis"
    permission_classes = [IsAcademicManager | IsAdvisorUser]

    def post(self, request, defense_id) -> Response:
        """
        Start the defense session.

        Args:
            request: HTTP request.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Updated defense payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        try:
            defense = start_defense(defense, actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"defense": defense_payload(defense, viewer=request.user)})


class DefenseCloseView(APIView):
    """
    Close an in-progress defense and compute the final result.

    Admins, coordinators and the advisor of the thesis may call it; the object-level
    rule (advisor of this thesis) is enforced by the service via ``_can_manage_defense``.
    """

    throttle_scope = "thesis"
    permission_classes = [IsAcademicManager | IsAdvisorUser]

    def post(self, request, defense_id) -> Response:
        """
        Close the defense, compute the committee result, and update the thesis.

        Args:
            request: HTTP request.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Updated defense payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        try:
            defense = close_defense(defense, actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"defense": defense_payload(defense, viewer=request.user)})


class DefenseMemberCreateView(APIView):
    """
    Add an institutional user as a committee member.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser]

    def post(self, request, defense_id) -> Response:
        """
        Add an institutional committee member to the defense.

        Args:
            request: HTTP request with member data.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Created member payload (201).
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        serializer = AddInstitutionalMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            member = add_institutional_member(defense, data["role"], data["user"], conflict_reason=data.get("conflict_reason", ""), actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"member": defense_member_payload(member, viewer=request.user)}, status=status.HTTP_201_CREATED)


class DefenseExternalMemberCreateView(APIView):
    """
    Add an external (non-institutional) user as a committee member via invitation.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser]

    def post(self, request, defense_id) -> Response:
        """
        Add an external committee member and create their invitation.

        The readable ``accessToken`` is returned only in this response (the database
        keeps only its hash) and the response is marked ``Cache-Control: no-store``.

        Args:
            request: HTTP request with external member data.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Created member payload plus ``accessToken`` and ``accessTokenExpiresAt`` (201).
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        serializer = AddExternalMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            member, access_token = add_external_member(defense, data["role"], name=data["name"], email=data["email"], institution=data.get("institution", ""), conflict_reason=data.get("conflict_reason", ""), actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return _external_token_response(member, access_token, request.user, status.HTTP_201_CREATED)


class DefenseExternalMemberTokenView(APIView):
    """
    Revoke an external member's invitation and issue a new access token.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser]

    def post(self, request, defense_id, member_id) -> Response:
        """
        Regenerate the external member's access token (same permission as managing the defense).

        Args:
            request: HTTP request.
            defense_id: Defense primary key from URL.
            member_id: DefenseMember primary key from URL.

        Returns:
            Response: Member payload plus the new ``accessToken`` and ``accessTokenExpiresAt``.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        member = get_object_or_404(DefenseMember, pk=member_id, defense=defense)
        try:
            member, access_token = regenerate_external_member_token(defense, member, actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return _external_token_response(member, access_token, request.user, status.HTTP_200_OK)


class DefenseMemberDeleteView(APIView):
    """
    Remove a committee member from a scheduled defense.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalUser]

    def delete(self, request, defense_id, member_id) -> Response:
        """
        Remove a committee member.

        Args:
            request: HTTP request.
            defense_id: Defense primary key from URL.
            member_id: DefenseMember primary key from URL.

        Returns:
            Response: Empty 204 response on success.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        member = get_object_or_404(DefenseMember, pk=member_id, defense=defense)
        try:
            remove_member(defense, member, actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DefenseEvaluationView(APIView):
    """
    Retrieve or submit a committee member's evaluation.
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalAdmin | IsDefenseCommitteeMember]

    def get(self, request, defense_id, member_id) -> Response:
        """
        Return the evaluation for a committee member.

        Full details (score, notes) are only shown to the member themselves or an admin.
        Other viewers receive decision and timestamp only.

        Args:
            request: HTTP request.
            defense_id: Defense primary key from URL.
            member_id: DefenseMember primary key from URL.

        Returns:
            Response: Evaluation payload, or 404 when not yet submitted.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        member = get_object_or_404(DefenseMember, pk=member_id, defense=defense)
        viewer_member = resolve_committee_member(defense, request.user)
        is_own = (viewer_member is not None and viewer_member.pk == member.pk) or (not isinstance(request.user, ExternalInvitationPrincipal) and user_is_admin(request.user))
        try:
            evaluation = member.evaluation
        except ObjectDoesNotExist:
            return Response({"detail": "Avaliação não submetida."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"evaluation": defense_evaluation_payload(evaluation, is_own=is_own)})

    def put(self, request, defense_id, member_id) -> Response:
        """
        Submit or update a committee member's evaluation.

        The evaluation and its event are recorded in one transaction. The result
        is calculated asynchronously by the adjudication consumer when the last
        required evaluation arrives; ``resultCalculationQueued`` confirms that
        the event was recorded.

        Args:
            request: HTTP request with evaluation data.
            defense_id: Defense primary key from URL.
            member_id: DefenseMember primary key from URL.

        Returns:
            Response: Evaluation payload plus ``resultCalculationQueued``, ``defenseStatus`` and ``defenseStatusLabel``.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        member = get_object_or_404(DefenseMember, pk=member_id, defense=defense)
        serializer = SubmitEvaluationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            outcome = record_committee_evaluation(defense, member, decision=data["decision"], score=data.get("score"), notes=data.get("notes", ""), actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        defense.refresh_from_db(fields=["status"])
        return Response({"evaluation": defense_evaluation_payload(outcome.evaluation, is_own=True), "resultCalculationQueued": outcome.result_calculation_queued, "defenseStatus": defense.status, "defenseStatusLabel": DefenseStatus(defense.status).label})


class DefenseMemberAvailabilityView(APIView):
    """
    Let a committee member confirm or decline availability for a scheduled defense.
    """

    throttle_scope = "thesis"
    permission_classes = [IsDefenseCommitteeMember]

    def post(self, request, defense_id, member_id) -> Response:
        """
        Record the member's own availability answer.

        Only the member themselves may answer (institutional user or the external
        session of their invitation); answering for another member returns 403.

        Args:
            request: HTTP request with ``status`` (CONFIRMED/DECLINED) and optional ``note``.
            defense_id: Defense primary key from URL.
            member_id: DefenseMember primary key from URL.

        Returns:
            Response: Updated member payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        member = get_object_or_404(DefenseMember, pk=member_id, defense=defense)
        serializer = AvailabilityResponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            member = respond_availability(defense, member, availability_status=data["status"], note=data["note"], actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"member": defense_member_payload(member, viewer=request.user)})


class DefenseRevertStatusView(APIView):
    """
    Revert a defense to its previous status (admin only).
    """

    throttle_scope = "thesis"
    permission_classes = [IsInstitutionalAdmin]

    def post(self, request, defense_id) -> Response:
        """
        Revert an IN_PROGRESS or CANCELLED defense to its previous status.

        Args:
            request: HTTP request with mandatory ``reason`` field.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Updated defense payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        try:
            defense = revert_defense_status(defense, reason=(request.data.get("reason") or ""), actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"defense": defense_payload(defense, viewer=request.user)})
