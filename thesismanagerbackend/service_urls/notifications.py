"""HTTP API of the notification service (delivery itself is queue-driven)."""

from django.urls import path

from accounts.views import HealthView
from notifications.views import NotificationListView

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    path("api/v1/notifications", NotificationListView.as_view(), name="api_notifications"),
]
