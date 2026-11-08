import uuid

import django.db.models.deletion
import django.db.models.expressions
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Create initial academic registry tables.
    """

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Department",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Departamento",
                "verbose_name_plural": "Departamentos",
                "db_table": "academic_department",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Course",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=150)),
                ("code", models.CharField(max_length=20)),
                (
                    "coordinator_email",
                    models.EmailField(blank=True, default="", max_length=254),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "coordinator",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="coordinated_courses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="courses",
                        to="academic.department",
                    ),
                ),
            ],
            options={
                "verbose_name": "Curso",
                "verbose_name_plural": "Cursos",
                "db_table": "academic_course",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="UserCourseAssignment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("assigned_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_course_memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "course",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="user_assignments",
                        to="academic.course",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="course_assignments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Associação usuário-curso",
                "verbose_name_plural": "Associações usuário-curso",
                "db_table": "academic_user_course_assignment",
                "ordering": ["-assigned_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="department",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                name="academic_department_name_ci_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="course",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("code"),
                name="academic_course_code_ci_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="usercourseassignment",
            constraint=models.UniqueConstraint(
                condition=django.db.models.expressions.Q(("is_active", True)),
                fields=("user",),
                name="academic_one_active_course_per_user",
            ),
        ),
        migrations.AddIndex(
            model_name="usercourseassignment",
            index=models.Index(
                fields=["course", "is_active"],
                name="academic_course_active_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="usercourseassignment",
            index=models.Index(
                fields=["user", "is_active"],
                name="academic_user_course_idx",
            ),
        ),
    ]
