from django.urls import path

from .views import (
    ExportSapReportAPI,
    RunSapReportAPI,
    SapReportCategoriesAPI,
    SapReportDetailAPI,
    SapReportListAPI,
    SapReportParameterOptionsAPI,
    SapReportRunHistoryAPI,
    SapReportSqlAPI,
    SyncSapReportsAPI,
)

urlpatterns = [
    path("reports/", SapReportListAPI.as_view(), name="sap-reports-list"),
    path("reports/<slug:slug>/", SapReportDetailAPI.as_view(), name="sap-reports-detail"),
    path("reports/<slug:slug>/sql/", SapReportSqlAPI.as_view(), name="sap-reports-sql"),
    path("reports/<slug:slug>/run/", RunSapReportAPI.as_view(), name="sap-reports-run"),
    path("reports/<slug:slug>/export/", ExportSapReportAPI.as_view(), name="sap-reports-export"),
    path(
        "reports/<slug:slug>/parameters/<int:position>/options/",
        SapReportParameterOptionsAPI.as_view(),
        name="sap-reports-parameter-options",
    ),
    path(
        "reports/<slug:slug>/runs/",
        SapReportRunHistoryAPI.as_view(),
        name="sap-reports-report-runs",
    ),
    path("runs/", SapReportRunHistoryAPI.as_view(), name="sap-reports-runs"),
    path("categories/", SapReportCategoriesAPI.as_view(), name="sap-reports-categories"),
    path("sync/", SyncSapReportsAPI.as_view(), name="sap-reports-sync"),
]
