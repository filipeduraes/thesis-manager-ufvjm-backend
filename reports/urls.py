from django.urls import path

from reports.views import ReportsOverviewPdfView, ReportsOverviewView

urlpatterns = [
    path("api/v1/reports/overview", ReportsOverviewView.as_view(), name="api_reports_overview"),
    path("api/v1/reports/overview/pdf", ReportsOverviewPdfView.as_view(), name="api_reports_overview_pdf"),
]
