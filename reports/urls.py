from django.urls import path

from reports.views import ReportsOverviewCsvView, ReportsOverviewPdfView, ReportsOverviewView

urlpatterns = [
    path("api/v1/reports/overview", ReportsOverviewView.as_view(), name="api_reports_overview"),
    path("api/v1/reports/overview/pdf", ReportsOverviewPdfView.as_view(), name="api_reports_overview_pdf"),
    path("api/v1/reports/overview/csv", ReportsOverviewCsvView.as_view(), name="api_reports_overview_csv"),
]
