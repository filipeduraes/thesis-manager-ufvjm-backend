"""
Views for the academic administrative API.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from academic.models import Course, Department
from academic.serializers import (
    CourseUpdateSerializer,
    CourseWriteSerializer,
    DepartmentWriteSerializer,
)
from academic.services import (
    course_payload,
    create_course,
    delete_course,
    department_payload,
    get_or_create_department,
    prune_orphan_departments,
    update_course,
)
from accounts.models import AuditAction
from accounts.permissions import IsInstitutionalAdmin
from accounts.services import record_audit, user_avatar_url


class AdminPagination(PageNumberPagination):
    """
    Administrative pagination with explicit frontend metadata.
    """

    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        """
        Return a paginated response with frontend-friendly metadata.

        Args:
            data: Serialized page results.

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


class AdminDepartmentsView(APIView):
    """
    List and create administrative departments.
    """

    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def get(self, request) -> Response:
        """
        List departments for administrative use.

        Args:
            request: HTTP request.

        Returns:
            Response: Department list payload.
        """
        queryset = Department.objects.all().order_by("name")
        query = request.query_params.get("q") or request.query_params.get("search")
        if query:
            queryset = queryset.filter(name__icontains=query.strip())
        selectable = request.query_params.get("selectable")
        if str(selectable).lower() in {"1", "true", "yes"}:
            # Includes departments referenced by courses; orphans are pruned elsewhere.
            queryset = queryset.filter(courses__isnull=False).distinct()
        results = [department_payload(item) for item in queryset]
        return Response({"results": results, "count": len(results)})

    @transaction.atomic
    def post(self, request) -> Response:
        """
        Create or reuse a department.

        Args:
            request: HTTP request.

        Returns:
            Response: Created or existing department payload.
        """
        serializer = DepartmentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        department = get_or_create_department(serializer.validated_data["name"])
        record_audit(
            action=AuditAction.CREATE,
            entity_type="Department",
            entity_id=str(department.pk),
            entity_app_label="academic",
            request=request,
            actor_user=request.user if request.user.is_authenticated else None,
            changes={"name": department.name},
        )
        return Response(department_payload(department), status=status.HTTP_201_CREATED)


class AdminDepartmentDetailView(APIView):
    """
    Update or delete an administrative department.
    """

    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    @transaction.atomic
    def patch(self, request, department_id) -> Response:
        """
        Update a department.

        Args:
            request: HTTP request.
            department_id: Department identifier.

        Returns:
            Response: Updated department payload or validation error.
        """
        department = get_object_or_404(
            Department.objects.select_for_update(), pk=department_id
        )
        serializer = DepartmentWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data.get("name")
        if name:
            if Department.objects.filter(name__iexact=name).exclude(
                pk=department.pk
            ).exists():
                return Response(
                    {"detail": "Já existe um departamento com este nome."},
                    status=status.HTTP_409_CONFLICT,
                )
            department.name = name
            department.save(update_fields=["name", "updated_at"])
            record_audit(
                action=AuditAction.UPDATE,
                entity_type="Department",
                entity_id=str(department.pk),
                entity_app_label="academic",
                request=request,
                actor_user=request.user if request.user.is_authenticated else None,
                changes={"name": department.name},
            )
        return Response(department_payload(department))

    @transaction.atomic
    def delete(self, request, department_id) -> Response:
        """
        Delete a department when it has no associated courses.

        Args:
            request: HTTP request.
            department_id: Department identifier.

        Returns:
            Response: Empty response or validation error.
        """
        department = get_object_or_404(Department, pk=department_id)
        if department.courses.exists():
            return Response(
                {
                    "detail": "Não é possível excluir um departamento associado a cursos."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        department_id_str = str(department.pk)
        name = department.name
        department.delete()
        record_audit(
            action=AuditAction.DELETE,
            entity_type="Department",
            entity_id=department_id_str,
            entity_app_label="academic",
            request=request,
            actor_user=request.user if request.user.is_authenticated else None,
            changes={"name": name},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminCoursesView(APIView):
    """
    List and create administrative courses.
    """

    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def get(self, request) -> Response:
        """
        List courses with search, department filter, and pagination.

        Args:
            request: HTTP request.

        Returns:
            Response: Paginated course payload.
        """
        queryset = Course.objects.select_related(
            "department", "coordinator"
        ).order_by("name")
        query = request.query_params.get("q") or request.query_params.get("search")
        department = request.query_params.get("department") or request.query_params.get(
            "departmentId"
        )
        if query:
            term = query.strip()
            queryset = queryset.filter(
                Q(name__icontains=term) | Q(code__icontains=term)
            )
        if department:
            queryset = queryset.filter(
                Q(department_id=department) | Q(department__name__iexact=department)
            )
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = [course_payload(course) for course in page]
        return paginator.get_paginated_response(data)

    def post(self, request) -> Response:
        """
        Create a course.

        Args:
            request: HTTP request.

        Returns:
            Response: Created course payload or validation error.
        """
        serializer = CourseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            course = create_course(request=request, **serializer.validated_data)
        except ValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        course = Course.objects.select_related("department", "coordinator").get(
            pk=course.pk
        )
        return Response(course_payload(course), status=status.HTTP_201_CREATED)


class AdminCourseDetailView(APIView):
    """
    Update and delete administrative courses.
    """

    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def get_object(self, course_id):
        """
        Load a course with related administrative data.

        Args:
            course_id: Course identifier.

        Returns:
            Course: Course instance.
        """
        return get_object_or_404(
            Course.objects.select_related("department", "coordinator"), pk=course_id
        )

    def patch(self, request, course_id) -> Response:
        """
        Update a course.

        Args:
            request: HTTP request.
            course_id: Course identifier.

        Returns:
            Response: Updated course payload or validation error.
        """
        course = self.get_object(course_id)
        serializer = CourseUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            course = update_course(course, request=request, **serializer.validated_data)
        except ValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        course = self.get_object(course.pk)
        return Response(course_payload(course))

    def delete(self, request, course_id) -> Response:
        """
        Delete a course.

        Args:
            request: HTTP request.
            course_id: Course identifier.

        Returns:
            Response: Empty response or validation error.
        """
        course = self.get_object(course_id)
        try:
            delete_course(course, request=request)
        except ValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        prune_orphan_departments()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminUserLookupView(APIView):
    """
    Look up an administrative user by e-mail.
    """

    permission_classes = [IsInstitutionalAdmin]
    throttle_scope = "session"

    def get(self, request) -> Response:
        """
        Check whether an institutional user exists.

        Args:
            request: HTTP request.

        Returns:
            Response: Lookup result for the coordinator e-mail field.
        """
        from accounts.models import User

        email = (request.query_params.get("email") or "").strip().lower()
        if not email:
            return Response(
                {"found": False, "user": None, "message": "Informe um e-mail."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return Response(
                {
                    "found": False,
                    "user": None,
                    "message": "Usuário ainda não entrou no sistema.",
                }
            )
        return Response(
            {
                "found": True,
                "user": {
                    "id": str(user.pk),
                    "name": user.name or user.email,
                    "email": user.email,
                    "isActive": user.is_active,
                    "avatarUrl": user_avatar_url(user),
                },
                "message": f"Usuário encontrado: {user.name or user.email}.",
            }
        )
