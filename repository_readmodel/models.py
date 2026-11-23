"""
Read model of the public repository.

These tables are owned by the repository service and rebuilt from domain
events. They contain only publicly safe bibliographic data (never e-mails), so
anonymous searches never touch the transactional database.
"""

from __future__ import annotations

import uuid

from django.db import models


class PublicCourse(models.Model):
    """Course as seen by the public repository."""

    id = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=30)
    published_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "repo_course"
        ordering = ["name"]

    def __str__(self) -> str:
        """Return the course name."""
        return self.name


class PublicAdvisor(models.Model):
    """Advisor as seen by the public repository."""

    id = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=200)
    avatar_url = models.CharField(max_length=500, blank=True, default="")
    published_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "repo_advisor"
        ordering = ["name"]

    def __str__(self) -> str:
        """Return the advisor name."""
        return self.name


class PublicKeyword(models.Model):
    """Keyword with the number of published theses using it."""

    name = models.CharField(primary_key=True, max_length=100)
    value = models.CharField(max_length=100)
    published_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "repo_keyword"
        ordering = ["name"]

    def __str__(self) -> str:
        """Return the keyword name."""
        return self.name


class PublicThesis(models.Model):
    """Published thesis projected into the repository read model."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_code = models.CharField(max_length=50)
    title = models.CharField(max_length=500)
    abstract = models.TextField(blank=True, default="")
    course = models.ForeignKey(PublicCourse, on_delete=models.SET_NULL, null=True, blank=True, related_name="theses")
    advisor = models.ForeignKey(PublicAdvisor, on_delete=models.SET_NULL, null=True, blank=True, related_name="theses")
    author_names = models.JSONField(default=list)
    keywords = models.JSONField(default=list)
    defense_date = models.DateField(null=True, blank=True)
    defense_year = models.PositiveSmallIntegerField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    search_text = models.TextField(blank=True, default="")
    pdf_sha256 = models.CharField(max_length=64)
    pdf_size_bytes = models.BigIntegerField(default=0)
    pdf_version = models.PositiveIntegerField(default=1)
    file_versions = models.JSONField(default=list)
    origin = models.CharField(max_length=20, default="WORKFLOW")

    class Meta:
        db_table = "repo_thesis"
        ordering = ["-published_at"]
        indexes = [
            models.Index(fields=["published_at"], name="repo_thesis_published_idx"),
            models.Index(fields=["defense_year"], name="repo_thesis_year_idx"),
        ]

    def __str__(self) -> str:
        """Return the thesis title."""
        return self.title
