from django.urls import path

from .views import BudgetApprovalColumnValuesAPI, BudgetApprovalReportAPI

urlpatterns = [
    path("report/", BudgetApprovalReportAPI.as_view(), name="budget-approvals-report"),
    path(
        "column-values/",
        BudgetApprovalColumnValuesAPI.as_view(),
        name="budget-approvals-column-values",
    ),
]
