from django.urls import path

from .views import (
    BudgetListAPI,
    CompanyListAPI,
    ExpenseClaimAttachmentAPI,
    ExpenseClaimAttachmentDetailAPI,
    ExpenseClaimDecideAPI,
    ExpenseClaimDetailAPI,
    ExpenseClaimListCreateAPI,
    GLAccountSearchAPI,
)

urlpatterns = [
    path("claims/", ExpenseClaimListCreateAPI.as_view(), name="expense-claims"),
    path("claims/<int:pk>/", ExpenseClaimDetailAPI.as_view(), name="expense-claim-detail"),
    path("claims/<int:pk>/decide/", ExpenseClaimDecideAPI.as_view(), name="expense-claim-decide"),
    path(
        "claims/<int:pk>/attachments/",
        ExpenseClaimAttachmentAPI.as_view(),
        name="expense-claim-attachments",
    ),
    path(
        "attachments/<int:pk>/",
        ExpenseClaimAttachmentDetailAPI.as_view(),
        name="expense-claim-attachment-detail",
    ),
    path("companies/", CompanyListAPI.as_view(), name="expense-claim-companies"),
    path("budgets/", BudgetListAPI.as_view(), name="expense-claim-budgets"),
    path("gl-accounts/", GLAccountSearchAPI.as_view(), name="expense-claim-gl-accounts"),
]
