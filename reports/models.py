"""
History of report generations.

The reports service owns this table. Actor identity is stored as a UUID plus
snapshot fields because users live in another service's database.
"""

from __future__ import annotations

import uuid

from django.db import models


class ReportExecution(models.Model):
    """One report generation requested by an institutional user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_id = models.UUIDField(db_index=True)
    actor_name = models.CharField(max_length=200, blank=True, default="")
    format = models.CharField(max_length=10)
    scope = models.CharField(max_length=30)
    filters = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "report_execution"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["actor_id", "created_at"], name="report_exec_actor_idx"),
        ]

    def __str__(self) -> str:
        """Return a readable execution identifier."""
        return f"{self.format} {self.scope} {self.created_at:%Y-%m-%d %H:%M}"
