"""
API views for authentication, session management and administration.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from allauth.socialaccount.adapter import get_adapter as get_social_adapter
from django.core.exceptions import ValidationError
from django.middleware.csrf import get_token
from allauth.socialaccount.providers import registry
from django.conf import settings
from django.contrib.auth import login as auth_login, logout
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import ExternalInvitationPrincipal
from accounts.audit_presentation import AuditPresenter, CATEGORY_LABELS, TRACE_ACTIONS
from accounts.models import (
    AuditAction,
    AuditLog,
    ExternalInvitation,
    ProfileRole,
    User,
    UserProfileRole,
)
from accounts.permissions import CanInviteExternalCommitteeMember, IsInstitutionalAdmin, IsInstitutionalManager, IsCoordinatorOrAdmin
from accounts.directory import ROLE_FILTERS
from accounts.serializers import (
    AdminUserCreateSerializer,
    AdminUserReadSerializer,
    AdminUserUpdateSerializer,
    AuditLogSerializer,
    ExternalTokenSerializer,
    ExternalInvitationCreateSerializer,
)
from accounts.services import (
    ExternalTokenError,
    LastAdminError,
    UserAlreadyExistsError,
    build_session_payload,
    create_external_invitation,
    create_institutional_user,
    deactivate_user,
    exchange_external_token,
    external_invitation_assignments,
    get_active_role,
    normalize_role,
    record_audit,
    revoke_external_invitation,
    role_label,
    set_user_roles,
    suggest_users,
)

logger = logging.getLogger(__name__)


class AdminPagination(PageNumberPagination):
    """Paginação administrativa com metadados explícitos."""

    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
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


class HealthView(APIView):
    """
    Return a minimal service health response.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "session"

    def get(self, request) -> Response:
        """
        Handle health-check requests.

        Args:
            request: HTTP request.

        Returns:
            Response: Health status payload.
        """
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class SessionView(APIView):
    """
    Return or destroy the current session.
    """
    permission_classes = [AllowAny]
    throttle_scope = "session"

    def get(self, request) -> Response:
        csrf_token = get_token(request._request)
        payload = build_session_payload(request)
        payload["csrfToken"] = csrf_token
        return Response(payload, status=status.HTTP_200_OK)

    def delete(self, request) -> Response:
        user = getattr(request, "user", None)
        if isinstance(user, ExternalInvitationPrincipal):
            invitation = getattr(request, "external_invitation", None)
            request.session.flush()
            try:
                record_audit(
                    action=AuditAction.LOGOUT,
                    entity_type="ExternalInvitation",
                    entity_id=str(invitation.pk) if invitation else "unknown",
                    entity_app_label="accounts",
                    request=request,
                    actor_external_invitation=invitation,
                )
            except Exception:
                logger.exception(
                    "Failed to record external logout audit: invitation_id=%s",
                    getattr(invitation, "pk", None),
                )
        else:
            if user and user.is_authenticated:
                logout(request)
            else:
                request.session.flush()

        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(settings.SESSION_COOKIE_NAME)
        return response


class GoogleStartView(APIView):
    """
    Initiate Google OAuth using django-allauth's headless/redirect flow.
    """
    permission_classes = [AllowAny]
    throttle_scope = "session"

    def get(self, request) -> Response:
        next_url = self._safe_next_url(request.query_params.get("next"))
        provider_class = registry.get_class("google")
        if not provider_class:
            return Response(
                {"detail": "Provedor Google não disponível."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            adapter = get_social_adapter(request)
            apps = adapter.list_apps(request, provider="google")
            app = apps[0] if apps else None
            provider = provider_class(request=request, app=app)
            request.GET = request.GET.copy()
            request.GET["process"] = "login"
            request.GET["next"] = next_url
            try:
                redirect = provider.redirect(request, process="login")
            except TypeError:
                redirect = provider.redirect(request, process="login", next_url=next_url)
        except Exception:
            logger.exception("Failed to start Google authentication.")
            return Response(
                {"detail": "Não foi possível iniciar a autenticação com o Google."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"url": redirect.url}, status=status.HTTP_200_OK)

    @staticmethod
    def _safe_next_url(candidate: str | None) -> str:
        frontend_url = settings.FRONTEND_URL.rstrip("/")
        frontend = urlsplit(frontend_url)
        if candidate and url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={frontend.netloc},
            require_https=frontend.scheme == "https",
        ):
            return candidate
        return frontend_url


class ExternalTokenExchangeView(APIView):
    """
    Exchange a temporary invitation token for a session.
    """
    permission_classes = [AllowAny]
    throttle_scope = "external_token"

    def post(self, request) -> Response:
        serializer = ExternalTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            invitation = exchange_external_token(
                serializer.validated_data["token"], request
            )
        except ExternalTokenError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return Response(
            {
                "authenticated": True,
                "authType": "external",
                "externalInvitation": {
                    "id": str(invitation.pk),
                    "name": invitation.name,
                    "email": invitation.email,
                    "institution": invitation.institution,
                    "expiresAt": invitation.expires_at.isoformat(),
                    "assignments": external_invitation_assignments(invitation),
                },
            },
            status=status.HTTP_200_OK,
        )


class AdminUsersView(APIView):
    """
    List and create users in the administrative area.
    """
    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def _queryset(self):
        prefetches = [
            Prefetch(
                "profile_roles",
                queryset=UserProfileRole.objects.filter(revoked_at__isnull=True),
                to_attr="active_role_records",
            ),
        ]
        try:
            from academic.models import UserCourseAssignment

            prefetches.append(
                Prefetch(
                    "course_assignments",
                    queryset=UserCourseAssignment.objects.filter(
                        is_active=True, revoked_at__isnull=True
                    ).select_related("course"),
                    to_attr="active_course_assignment_records",
                )
            )
        except Exception:
            pass
        try:
            from allauth.socialaccount.models import SocialAccount

            prefetches.append(
                Prefetch(
                    "socialaccount_set",
                    queryset=SocialAccount.objects.filter(provider="google"),
                    to_attr="google_social_accounts",
                )
            )
        except Exception:
            pass
        return User.objects.prefetch_related(*prefetches).order_by("email")

    def get(self, request) -> Response:
        queryset = self._queryset()
        role_filter = request.query_params.get("role")
        status_filter = request.query_params.get("status")
        search = request.query_params.get("search") or request.query_params.get(
            "q"
        )

        if role_filter:
            normalized_role = role_filter.strip().lower()
            role_code = None
            for role in ProfileRole:
                if role.value.lower() == normalized_role or role.label.lower() == normalized_role:
                    role_code = role.value
                    break
            if role_code:
                queryset = queryset.filter(
                    profile_roles__role=role_code,
                    profile_roles__revoked_at__isnull=True,
                ).distinct()

        if status_filter:
            normalized_status = status_filter.strip().lower()
            if normalized_status in {"inativo", "inactive"}:
                queryset = queryset.filter(is_active=False)
            elif normalized_status in {"ativo", "active", "pendente"}:
                if normalized_status in {"ativo", "active"}:
                    queryset = queryset.filter(is_active=True)
                else:
                    queryset = queryset.filter(is_active=False)

        if search:
            term = search.strip()
            queryset = queryset.filter(
                Q(name__icontains=term) | Q(email__icontains=term)
            )

        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = AdminUserReadSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request) -> Response:
        serializer = AdminUserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                user = create_institutional_user(
                    email=serializer.validated_data["email"],
                    name=serializer.validated_data["name"],
                    roles=serializer.validated_data["roles"],
                    assigned_by=request.user if request.user.is_authenticated else None,
                    request=request,
                )
                course_id = serializer.validated_data.get("courseId")
                if course_id:
                    from academic.models import Course
                    from academic.services import set_user_course_assignment

                    course = Course.objects.filter(pk=course_id).first()
                    if course:
                        set_user_course_assignment(
                            user,
                            course,
                            assigned_by=request.user if request.user.is_authenticated else None,
                            request=request,
                        )
        except UserAlreadyExistsError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except ValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

        user = self._queryset().get(pk=user.pk)
        serializer = AdminUserReadSerializer(user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AdminUserDetailView(APIView):
    """
    Retrieve, update and deactivate an administrative user.
    """
    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def get_object(self, user_id):
        prefetches = [
            Prefetch(
                "profile_roles",
                queryset=UserProfileRole.objects.filter(revoked_at__isnull=True),
                to_attr="active_role_records",
            ),
        ]
        try:
            from academic.models import UserCourseAssignment

            prefetches.append(
                Prefetch(
                    "course_assignments",
                    queryset=UserCourseAssignment.objects.filter(
                        is_active=True, revoked_at__isnull=True
                    ).select_related("course"),
                    to_attr="active_course_assignment_records",
                )
            )
        except Exception:
            pass
        try:
            from allauth.socialaccount.models import SocialAccount

            prefetches.append(
                Prefetch(
                    "socialaccount_set",
                    queryset=SocialAccount.objects.filter(provider="google"),
                    to_attr="google_social_accounts",
                )
            )
        except Exception:
            pass
        return get_object_or_404(
            User.objects.prefetch_related(*prefetches),
            pk=user_id,
        )

    def patch(self, request, user_id):
        user = self.get_object(user_id)
        serializer = AdminUserUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        audit_changes: dict[str, object] = {}
        previous_roles = [
            record.role
            for record in getattr(user, "active_role_records", []) or []
        ]
        previous_course_records = getattr(user, "active_course_assignment_records", []) or []
        previous_course = previous_course_records[0].course if previous_course_records else None
        previous_is_active = user.is_active

        try:
            with transaction.atomic():
                if "roles" in validated:
                    set_user_roles(
                        user,
                        validated["roles"],
                        assigned_by=request.user if request.user.is_authenticated else None,
                    )
                    audit_changes["roles"] = {
                        "from": previous_roles,
                        "to": validated["roles"],
                    }
                if "courseId" in request.data:
                    from academic.models import Course
                    from academic.services import set_user_course_assignment

                    raw_course = request.data.get("courseId")
                    course = None
                    if raw_course:
                        course = Course.objects.filter(pk=raw_course).first()
                        if not course:
                            return Response(
                                {"detail": "Curso inválido."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                    set_user_course_assignment(
                        user,
                        course,
                        assigned_by=request.user if request.user.is_authenticated else None,
                        request=request,
                    )
                    audit_changes["course"] = {
                        "from": previous_course.name if previous_course else None,
                        "to": course.name if course else None,
                    }
                if "is_active" in validated:
                    if validated["is_active"] and not user.is_active:
                        user.is_active = True
                        user.save(update_fields=["is_active"])
                        audit_changes["is_active"] = {
                            "from": previous_is_active,
                            "to": True,
                        }
                    elif not validated["is_active"] and user.is_active:
                        deactivate_user(
                            user,
                            actor=request.user if request.user.is_authenticated else None,
                            request=request,
                        )
                        # deactivate_user já registra a exclusão lógica.
                        audit_changes = {}
                if audit_changes:
                    record_audit(
                        action=AuditAction.UPDATE,
                        entity_type="User",
                        entity_id=str(user.pk),
                        entity_app_label="accounts",
                        request=request,
                        actor_user=request.user if request.user.is_authenticated else None,
                        changes=audit_changes,
                        metadata={"name": user.name, "email": user.email},
                    )
        except (LastAdminError, ValidationError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": str(exc)}
            return Response(
                detail,
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = self.get_object(user_id)
        serializer = AdminUserReadSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, user_id):
        user = self.get_object(user_id)
        try:
            deactivate_user(
                user,
                actor=request.user if request.user.is_authenticated else None,
                request=request,
            )
        except (LastAdminError, ValidationError) as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": str(exc)}
            return Response(
                detail,
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminExternalInvitationsView(APIView):
    """List invitations and issue new committee access tokens."""
    throttle_scope = "session"

    def get_permissions(self):
        if self.request.method == "POST":
            return [CanInviteExternalCommitteeMember()]
        return [IsInstitutionalAdmin()]

    def get(self, request) -> Response:
        invitations = ExternalInvitation.objects.order_by("-created_at")[:100]
        results = [
            {
                "id": str(invitation.pk),
                "name": invitation.name,
                "email": invitation.email,
                "institution": invitation.institution,
                "status": invitation.status,
                "expiresAt": invitation.expires_at.isoformat(),
                "usedAt": invitation.used_at.isoformat() if invitation.used_at else None,
            }
            for invitation in invitations
        ]
        return Response({"results": results}, status=status.HTTP_200_OK)

    def post(self, request) -> Response:
        serializer = ExternalInvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation, token = create_external_invitation(
            name=serializer.validated_data["name"],
            email=serializer.validated_data["email"],
            institution=serializer.validated_data.get("institution", ""),
            invited_by=request.user,
            request=request,
        )
        return Response(
            {
                "id": str(invitation.pk),
                "name": invitation.name,
                "email": invitation.email,
                "institution": invitation.institution,
                "token": token,
                "expiresAt": invitation.expires_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )


class AdminExternalInvitationDetailView(APIView):
    """Revoke a committee invitation and its active sessions."""
    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def delete(self, request, invitation_id) -> Response:
        invitation = get_object_or_404(ExternalInvitation, pk=invitation_id)
        revoke_external_invitation(
            invitation,
            revoked_by=request.user,
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


def _filtered_audit_logs(request):
    queryset = AuditLog.objects.select_related(
        "actor_user",
        "actor_external_invitation",
    ).all()
    from_date = request.query_params.get("from")
    to_date = request.query_params.get("to")
    role_filter = request.query_params.get("role")
    query = request.query_params.get("q")
    raw_filter = (
        request.query_params.get("category")
        or request.query_params.get("verbosity")
        or "all"
    ).strip().lower()
    valid_categories = {c.lower() for c in CATEGORY_LABELS}
    valid_categories |= {"trace", "relevant", "all", "none"}
    if raw_filter not in {"all", "none"}:
        tokens = {token.strip() for token in raw_filter.split(",") if token.strip()}
        normalized: set[str] = set()
        for token in tokens:
            if token == "trace":
                normalized.add("access")
            elif token == "relevant":
                normalized.update({"management", "thesis", "publication"})
            elif token in {"auth", "access"}:
                normalized.add("access")
            elif token in {"admin", "management"}:
                normalized.add("management")
            elif token in {"thesis", "defense"}:
                normalized.add("thesis")
            elif token in {"repository", "report", "publication"}:
                normalized.add("publication")
            elif token == "error":
                normalized.add("error")
            else:
                normalized.add(token)
        tokens = normalized
        if not tokens or not tokens.issubset(valid_categories - {"all", "none", "trace", "relevant"}):
            return None, Response(
                {"category": "Use all, none ou códigos de categoria separados por vírgula."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from django.db.models import Q as _Q
        queryset = queryset.filter(_Q(category="") | _Q(category__in=[t.upper() for t in tokens]))
    elif raw_filter == "none":
        queryset = queryset.none()

    if from_date:
        if parse_date(from_date) is None:
            return None, Response(
                {"from": "Use a data no formato YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(occurred_at__date__gte=from_date)
    if to_date:
        if parse_date(to_date) is None:
            return None, Response(
                {"to": "Use a data no formato YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(occurred_at__date__lte=to_date)
    if role_filter:
        normalized_role = role_filter.strip().lower()
        if normalized_role == "sistema":
            queryset = queryset.filter(actor_user__isnull=True, actor_external_invitation__isnull=True)
        else:
            role_code = None
            for role in ProfileRole:
                if (
                    role.value.lower() == normalized_role
                    or role.label.lower() == normalized_role
                ):
                    role_code = role.value
                    break
            if role_code:
                queryset = queryset.filter(
                    Q(actor_role=role_code) | Q(actor_roles__contains=[role_code])
                )

    if query:
        term = query.strip()
        queryset = queryset.filter(
            Q(action__icontains=term)
            | Q(entity_id__icontains=term)
            | Q(entity_type__icontains=term)
            | Q(actor_user__name__icontains=term)
            | Q(actor_user__email__icontains=term)
            | Q(actor_external_invitation__name__icontains=term)
        )
    return queryset, None


class AdminAuditLogsView(APIView):
    """
    List audit logs in the administrative area.
    """
    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def get(self, request) -> Response:
        if (request.query_params.get("export") or "").strip().lower() in {"csv", "json"}:
            return AdminAuditLogsExportView().get(request)
        queryset, error_response = _filtered_audit_logs(request)
        if error_response:
            return error_response
        page = self._paginate(request, queryset)
        presenter = AuditPresenter()
        presenter.prime(page)
        serializer_data = [
            AuditLogSerializer(
                log,
                context={"audit_presenter": presenter, "request": request},
            ).data
            for log in page
        ]
        return self._paginated_response(request, serializer_data, page, queryset)

    def _paginate(self, request, queryset):
        from rest_framework.pagination import PageNumberPagination

        paginator = PageNumberPagination()
        paginator.page_size = 50
        paginator.page_size_query_param = "page_size"
        self.paginator = paginator
        return paginator.paginate_queryset(queryset, request, view=self)

    def _paginated_response(self, request, data, page, queryset):
        return self.paginator.get_paginated_response(data)


class AdminAuditLogsExportView(APIView):
    """
    Export filtered audit logs as a downloadable file.
    """
    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def get(self, request) -> Response:
        import csv
        import io
        import json
        from django.http import HttpResponse

        queryset, error_response = _filtered_audit_logs(request)
        if error_response:
            return error_response
        logs = list(queryset[:10000])
        presenter = AuditPresenter()
        presenter.prime(logs)
        data = [
            AuditLogSerializer(
                log,
                context={"audit_presenter": presenter, "request": request},
            ).data
            for log in logs
        ]
        export_format = (
            request.query_params.get("export")
            or request.query_params.get("format")
            or "csv"
        ).strip().lower()

        if export_format == "json":
            response = HttpResponse(
                json.dumps(data, ensure_ascii=False, indent=2),
                content_type="application/json; charset=utf-8",
            )
            response["Content-Disposition"] = 'attachment; filename="audit-logs.json"'
            return response

        output = io.StringIO()
        fieldnames = [
            "timestamp",
            "actor",
            "actorEmail",
            "role",
            "roles",
            "roleCodes",
            "action",
            "actionCode",
            "actionLabel",
            "category",
            "categoryLabel",
            "verbosity",
            "verbosityLabel",
            "summary",
            "entityType",
            "entityTypeLabel",
            "entityAppLabel",
            "entityId",
            "entityName",
            "entityReference",
            "origin",
            "hash",
            "details",
            "changes",
            "metadata",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow(
                {
                    **{key: row.get(key, "") for key in fieldnames},
                    "roles": "; ".join(
                        item.get("label", "")
                        for item in (row.get("roles") or [])
                    ),
                    "roleCodes": "; ".join(row.get("roleCodes") or []),
                    "details": json.dumps(row.get("details") or [], ensure_ascii=False),
                    "changes": json.dumps(row.get("changes") or {}, ensure_ascii=False),
                    "metadata": json.dumps(row.get("metadata") or {}, ensure_ascii=False),
                }
            )
        response = HttpResponse(
            "\ufeff" + output.getvalue(),
            content_type="text/csv; charset=utf-8",
        )
        response["Content-Disposition"] = 'attachment; filename="audit-logs.csv"'
        return response


def _dev_login_roles(payload: dict) -> list[str]:
    """
    Resolve os perfis solicitados no login de teste (somente desenvolvimento).

    Args:
        payload: Dados enviados pelo frontend.

    Returns:
        list[str]: Códigos de perfil válidos.
    """
    raw_roles = payload.get("roles")
    candidates = []
    if isinstance(raw_roles, (list, tuple)):
        candidates = list(raw_roles)
    elif raw_roles is not None:
        candidates = [raw_roles]
    if not candidates:
        raw = (
            payload.get("role")
            or payload.get("roleCode")
            or payload.get("profile")
            or payload.get("type")
            or ""
        )
        if raw:
            candidates = [raw]

    resolved: list[str] = []
    for raw in candidates:
        code = normalize_role(str(raw))
        if code in ProfileRole.values and code not in resolved:
            resolved.append(code)
            continue
        candidate = str(raw).strip().upper()
        if candidate in ProfileRole.values and candidate not in resolved:
            resolved.append(candidate)
    return resolved or [ProfileRole.STUDENT]


def _dev_login_user(email: str, name: str, roles: list[str], request) -> User:
    """
    Cria ou atualiza um usuário de teste e garante os perfis, sem validações extras.

    Args:
        email: E-mail informado.
        name: Nome informado.
        roles: Códigos de perfil a garantir.
        request: Requisição HTTP.

    Returns:
        User: Usuário autenticável para desenvolvimento.
    """
    from django.utils import timezone

    normalized_email = (email or "").strip().lower() or f"teste-{timezone.now().timestamp():.0f}@dev.local"
    display_name = (name or "").strip() or normalized_email.split("@", 1)[0]
    with transaction.atomic():
        user = User.objects.filter(email__iexact=normalized_email).first()
        if user is None:
            user = User(email=normalized_email, name=display_name, is_active=True)
            user.set_unusable_password()
            user.save()
            logger.info("Dev test user created: user_id=%s email=%s", user.pk, user.email)
        else:
            updates: list[str] = []
            if display_name and user.name != display_name:
                user.name = display_name
                updates.append("name")
            if not user.is_active:
                user.is_active = True
                updates.append("is_active")
            if updates:
                user.save(update_fields=updates)

        # Modo dev: garante os papéis solicitados, respeitando a exclusividade de Aluno.
        now = timezone.now()
        if ProfileRole.STUDENT in roles:
            user.profile_roles.filter(revoked_at__isnull=True).exclude(
                role=ProfileRole.STUDENT
            ).update(revoked_at=now)
            if not user.profile_roles.filter(role=ProfileRole.STUDENT, revoked_at__isnull=True).exists():
                UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        else:
            user.profile_roles.filter(
                role=ProfileRole.STUDENT, revoked_at__isnull=True
            ).update(revoked_at=now)
            for role in roles:
                if not user.profile_roles.filter(role=role, revoked_at__isnull=True).exists():
                    UserProfileRole.objects.create(user=user, role=role)
        has_admin_role = user.profile_roles.filter(
            role=ProfileRole.ADMIN, revoked_at__isnull=True
        ).exists()
        if has_admin_role and not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        elif not has_admin_role and user.is_staff and not user.is_superuser:
            user.is_staff = False
            user.save(update_fields=["is_staff"])
    return user


class DevLoginView(APIView):
    """
    Autentica como usuário de teste em ambiente de desenvolvimento.
    """

    permission_classes = [AllowAny]
    throttle_scope = "session"

    def post(self, request) -> Response:
        """
        Cria/atualiza e autentica um usuário de teste.

        Args:
            request: Requisição com ``name``, ``email`` e ``roles``.

        Returns:
            Response: Sessão autenticada.
        """
        if not settings.DEBUG:
            return Response(status=status.HTTP_404_NOT_FOUND)
        payload = request.data if isinstance(request.data, dict) else {}
        roles = _dev_login_roles(payload)
        if ProfileRole.STUDENT in roles and len(roles) > 1:
            return Response(
                {"detail": "O perfil Aluno não pode ser combinado com outros perfis."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        email = str(payload.get("email") or "").strip()
        name = str(payload.get("name") or "").strip()
        try:
            user = _dev_login_user(email, name, roles, request)
            auth_login(
                request._request,
                user,
                backend="django.contrib.auth.backends.ModelBackend",
            )
            request.user = user
            request._request.user = user
        except Exception:
            logger.exception("Dev login failed: email=%s roles=%s", email, roles)
            return Response(
                {"detail": "Não foi possível autenticar o usuário de teste."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        csrf_token = get_token(request._request)
        session_payload = build_session_payload(request)
        session_payload["csrfToken"] = csrf_token
        return Response(session_payload, status=status.HTTP_200_OK)


class DevUsersView(APIView):
    """
    Lista usuários disponíveis para login de teste em desenvolvimento.
    """

    permission_classes = [AllowAny]
    throttle_scope = "session"

    def get(self, request) -> Response:
        """
        Retorna usuários ativos com seus perfis.

        Args:
            request: Requisição HTTP.

        Returns:
            Response: Lista de usuários de teste.
        """
        if not settings.DEBUG:
            return Response(status=status.HTTP_404_NOT_FOUND)
        users = (
            User.objects.filter(is_active=True)
            .prefetch_related("profile_roles")
            .order_by("name", "email")
        )
        results = []
        for user in users:
            role = get_active_role(user)
            role_codes = [
                assignment.role
                for assignment in user.profile_roles.all()
                if assignment.revoked_at is None
            ]
            results.append(
                {
                    "id": str(user.pk),
                    "name": user.name or user.email,
                    "email": user.email,
                    "roleCode": role,
                    "roleLabel": role_label(role),
                    "roles": role_codes,
                }
            )
        return Response({"results": results}, status=status.HTTP_200_OK)


class UserSuggestView(APIView):
    """
    Return up to three institutional users matching a search fragment.

    Administrators and coordinators search the full directory; advisors see
    only other advisors (the advisors list is public). Students and external
    sessions receive 403.
    """

    permission_classes = [IsInstitutionalManager]
    throttle_scope = "user_suggest"

    def get(self, request) -> Response:
        """
        Return the matching users for an autocomplete field.

        Query strings shorter than two characters return an empty result list
        (never an error). Advisors only see other advisors; coordinators and
        administrators can optionally narrow the result with ``role``.

        Args:
            request: HTTP request with the ``q`` query parameter.

        Returns:
            Response: Up to three matches, ordered by name then e-mail.
        """
        query = (request.query_params.get("q") or "").strip()
        start_roles = None
        if not IsCoordinatorOrAdmin().has_permission(request, self):
            start_roles = (ProfileRole.ADVISOR,)
        else:
            role = (request.query_params.get("role") or "").strip().upper()
            if role and role not in ROLE_FILTERS:
                return Response({"detail": "Valor de role inválido."}, status=status.HTTP_400_BAD_REQUEST)
            start_roles = ROLE_FILTERS.get(role) if role else None
        return Response({"results": suggest_users(query, start_roles)}, status=status.HTTP_200_OK)
