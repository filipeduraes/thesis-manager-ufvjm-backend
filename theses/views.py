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
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.renderers import BaseRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditAction
from accounts.permissions import IsInstitutionalAdmin, IsInstitutionalUser
from accounts.services import record_audit
from academic.services import active_course_assignment
from theses.models import (
    REVIEWABLE_STATUSES,
    StoredFile,
    Thesis,
    ThesisDocument,
    ThesisFile,
    ThesisStatus,
)
from theses.permissions import IsAcademicManager, IsAdvisorUser, IsStudentUser
from theses.serializers import (
    DefenseResultSerializer,
    DocumentUploadSerializer,
    ThesisMetadataSerializer,
    ThesisReviewSerializer,
)
from theses.services import (
    available_advisors,
    available_courses_for_student,
    course_brief,
    document_payload,
    get_accessible_thesis,
    public_theses_filtered,
    public_thesis_payload,
    publish_thesis,
    record_defense_result,
    review_payload,
    review_thesis,
    save_thesis_metadata,
    submit_thesis,
    thesis_file_payload,
    withdraw_submission,
    thesis_list_payload,
    thesis_payload,
    theses_visible_to,
    upload_thesis_document,
    upload_thesis_file,
    user_brief,
    user_can_access_thesis,
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


def _scoped_theses(request) -> QuerySet[Thesis]:
    """
    Apply role, scope and filter parameters to the visible thesis queryset.

    Args:
        request: HTTP request.

    Returns:
        QuerySet[Thesis]: Filtered queryset.
    """
    queryset = theses_visible_to(request.user)
    scope = (request.query_params.get("scope") or "").strip().lower()
    if scope == "advised":
        queryset = queryset.filter(advisor_id=request.user.pk)
    elif scope == "authored":
        queryset = queryset.filter(authors__author_id=request.user.pk)
    elif scope == "coordinated":
        queryset = queryset.filter(
            Q(course__coordinator_id=request.user.pk)
            | Q(course__coordinator_email__iexact=request.user.email)
            | Q(
                course__user_assignments__user_id=request.user.pk,
                course__user_assignments__is_active=True,
                course__user_assignments__revoked_at__isnull=True,
            )
        )
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
        try:
            parsed_year = int(year)
        except (TypeError, ValueError):
            parsed_year = None
        if parsed_year is not None:
            queryset = queryset.filter(Q(submitted_at__year=parsed_year) | Q(published_at__year=parsed_year))
    pending_review = (request.query_params.get("pendingReview") or "").strip().lower()
    if pending_review in {"1", "true", "yes"}:
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
        .filter(authors__author=user, authors__is_main_author=True)
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
        results = [thesis_list_payload(thesis, request.user) for thesis in page]
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
        thesis = theses_visible_to(request.user).filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
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
        thesis = theses_visible_to(request.user).filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
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
        thesis = theses_visible_to(request.user).filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
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
        thesis = theses_visible_to(request.user).filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
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
        record_audit(
            action=AuditAction.DOWNLOAD,
            entity_type="ThesisFile",
            entity_id=str(thesis_file.pk),
            entity_app_label="theses",
            request=request,
            metadata={"thesisId": str(thesis.pk), "versionNumber": thesis_file.version_number, "disposition": disposition},
        )
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
        thesis = theses_visible_to(request.user).filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
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
        thesis = theses_visible_to(request.user).filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
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
        record_audit(
            action=AuditAction.DOWNLOAD,
            entity_type="ThesisDocument",
            entity_id=str(document.pk),
            entity_app_label="theses",
            request=request,
            metadata={"thesisId": str(thesis.pk), "documentType": document.type},
        )
        return _download_response(document.stored_file, thesis, "attachment")


class PublicThesisListView(APIView):
    """
    List published theses in the public repository.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request) -> Response:
        """
        Return published theses with public filters.

        Args:
            request: HTTP request.

        Returns:
            Response: Paginated public thesis summaries.
        """
        queryset = public_theses_filtered(
            query=(request.query_params.get("q") or "").strip(),
            course_id=(request.query_params.get("courseId") or "").strip(),
            advisor_id=(request.query_params.get("advisorId") or "").strip(),
            year=(request.query_params.get("year") or "").strip(),
            keyword=(request.query_params.get("keyword") or "").strip(),
        )
        paginator = ThesisPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        results = [thesis_list_payload(thesis, None) for thesis in page]
        return paginator.get_paginated_response(results)


class PublicThesisDetailView(APIView):
    """
    Return a published thesis from the public repository.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request, thesis_id) -> Response:
        """
        Return public thesis metadata.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            Response: Public thesis payload.
        """
        thesis = public_theses_filtered().filter(pk=thesis_id).first()
        if thesis is None:
            raise Http404("Monografia não encontrada.")
        return Response({"thesis": public_thesis_payload(thesis)})


class PublicThesisDownloadView(APIView):
    """
    Stream the current file of a published thesis.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    renderer_classes = [BinaryFileRenderer]

    def get(self, request, thesis_id) -> FileResponse:
        """
        Download or preview the current public PDF.

        Args:
            request: HTTP request.
            thesis_id: Thesis identifier.

        Returns:
            FileResponse: Streamed PDF.
        """
        thesis = public_theses_filtered().filter(pk=thesis_id).first()
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

from theses.defense_committee import add_external_member, add_institutional_member, remove_member, resolve_committee_member  # noqa: E402
from theses.defense_evaluation import defense_evaluation_payload, submit_committee_evaluation  # noqa: E402
from theses.defense_listing import DefenseListFilters, defense_list_payload, defenses_visible_to, list_visible_defenses, schedulable_theses_for  # noqa: E402
from theses.defense_scheduling import cancel_defense, close_defense, defense_payload, defense_status_history_payload, revert_defense_status, schedule_defense, start_defense, sync_defense_status, update_defense  # noqa: E402
from theses.models import Defense, DefenseMember  # noqa: E402
from theses.permissions import IsDefenseCommitteeMember  # noqa: E402
from theses.serializers import AddExternalMemberSerializer, AddInstitutionalMemberSerializer, ScheduleDefenseSerializer, SubmitEvaluationSerializer, UpdateDefenseSerializer  # noqa: E402
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

    - Admin / coordinator: any thesis.
    - Advisor: only theses they advise.
    - Student: only their authored (main) thesis.
    """
    if user_is_admin(user) or user_is_coordinator(user):
        return get_object_or_404(Thesis, pk=thesis_id)
    if user_is_advisor(user):
        return get_object_or_404(Thesis, pk=thesis_id, advisor_id=user.pk)
    return get_object_or_404(Thesis, pk=thesis_id, authors__author=user, authors__is_main_author=True)


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
        results = [defense_list_payload(defense, viewer=request.user) for defense in page]
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
        defenses = Defense.objects.filter(thesis=thesis).prefetch_related("members__institutional_member", "members__external_invitation").order_by("-scheduled_datetime")
        return Response({"defenses": [defense_payload(d, viewer=request.user) for d in defenses]})

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
        Return the defense with its committee members and chronological ``statusHistory``.

        Args:
            request: HTTP request.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Defense payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        detail_payload = defense_payload(defense, viewer=request.user)
        detail_payload["statusHistory"] = defense_status_history_payload(defense)
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
        from theses.defense_committee import defense_member_payload
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

        Args:
            request: HTTP request with external member data.
            defense_id: Defense primary key from URL.

        Returns:
            Response: Created member payload (201).
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        serializer = AddExternalMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            member = add_external_member(defense, data["role"], name=data["name"], email=data["email"], institution=data.get("institution", ""), conflict_reason=data.get("conflict_reason", ""), actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        from theses.defense_committee import defense_member_payload
        return Response({"member": defense_member_payload(member, viewer=request.user)}, status=status.HTTP_201_CREATED)


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

        Args:
            request: HTTP request with evaluation data.
            defense_id: Defense primary key from URL.
            member_id: DefenseMember primary key from URL.

        Returns:
            Response: Submitted evaluation payload.
        """
        defense = get_object_or_404(_scoped_defense_qs(request.user), pk=defense_id)
        defense = sync_defense_status(defense, request=request)
        member = get_object_or_404(DefenseMember, pk=member_id, defense=defense)
        serializer = SubmitEvaluationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            evaluation = submit_committee_evaluation(defense, member, decision=data["decision"], score=data.get("score"), notes=data.get("notes", ""), actor=request.user, request=request)
        except DjangoValidationError as exc:
            return _validation_response(exc)
        return Response({"evaluation": defense_evaluation_payload(evaluation, is_own=True)})


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
