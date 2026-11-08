"""
URL routes for the academic administrative API.
"""

from django.urls import path

from academic import views

urlpatterns = [
    path(
        "api/v1/admin/departments",
        views.AdminDepartmentsView.as_view(),
        name="api_admin_departments",
    ),
    path(
        "api/v1/admin/departments/<uuid:department_id>",
        views.AdminDepartmentDetailView.as_view(),
        name="api_admin_department_detail",
    ),
    path(
        "api/v1/admin/courses",
        views.AdminCoursesView.as_view(),
        name="api_admin_courses",
    ),
    path(
        "api/v1/admin/courses/<uuid:course_id>",
        views.AdminCourseDetailView.as_view(),
        name="api_admin_course_detail",
    ),
    path(
        "api/v1/admin/users/lookup",
        views.AdminUserLookupView.as_view(),
        name="api_admin_user_lookup",
    ),
]
