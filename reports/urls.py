from django.urls import path

from reports.views import ReportsOverviewView

urlpatterns = [
    path("api/v1/reports/overview", ReportsOverviewView.as_view(), name="api_reports_overview"),
]
