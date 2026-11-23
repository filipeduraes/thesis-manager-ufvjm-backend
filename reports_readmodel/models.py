"""
Denormalized facts used by the reporting service.

One row per thesis; populated from domain events so aggregate queries never
join the transactional tables.
"""

from __future__ import annotations

import uuid

from django.db import models


class ReportFact(models.Model):
    """Reporting fact of one thesis."""

    thesis_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_id = models.UUIDField(null=True, blank=True, db_index=True)
    course_name = models.CharField(max_length=200, blank=True, default="")
    course_code = models.CharField(max_length=30, blank=True, default="")
    advisor_id = models.UUIDField(null=True, blank=True, db_index=True)
    advisor_name = models.CharField(max_length=200, blank=True, default="")
    defense_year = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    defense_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=30, blank=True, default="")
    thesis_status = models.CharField(max_length=30, blank=True, default="")
    is_published = models.BooleanField(default=False)

    class Meta:
        db_table = "report_fact"
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["course_id", "thesis_status"], name="report_fact_course_status_idx"),
            models.Index(fields=["advisor_id", "thesis_status"], name="report_fact_advisor_status_idx"),
        ]

    def __str__(self) -> str:
        """Return the thesis identifier."""
        return str(self.thesis_id)
