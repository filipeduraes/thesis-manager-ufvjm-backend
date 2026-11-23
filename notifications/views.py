"""
HTTP API of the notifications service.

E-mail delivery is queue-driven; this read-only endpoint lets a signed-in user
inspect the messages produced for them by the notification consumer.
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.models import Notification, NotificationStatus


class NotificationListView(APIView):
    """List the notifications addressed to the signed-in user."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "session"

    def get(self, request) -> Response:
        """
        Return the caller's notifications, newest first.

        Args:
            request: HTTP request.

        Returns:
            Response: Notification summaries.
        """
        items = (
            Notification.objects.filter(user=request.user)
            .order_by("-created_at")[:50]
            .values("id", "subject", "status", "created_at", "sent_at")
        )
        results = [
            {
                "id": str(item["id"]),
                "subject": item["subject"],
                "status": item["status"],
                "statusLabel": NotificationStatus(item["status"]).label,
                "createdAt": item["created_at"].isoformat() if item["created_at"] else None,
                "sentAt": item["sent_at"].isoformat() if item["sent_at"] else None,
            }
            for item in items
        ]
        return Response({"count": len(results), "results": results})
