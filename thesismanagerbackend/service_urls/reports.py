"""HTTP API of the reporting service."""

from django.urls import path

from accounts.views import HealthView
from reports_readmodel.views import OverviewReadModelView

urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    path("api/v1/reports/overview", OverviewReadModelView.as_view(), name="api_reports_overview"),
]
