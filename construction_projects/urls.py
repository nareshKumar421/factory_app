from django.urls import path

from .views import (
    ApprovalQueueAPI,
    DailyLogDetailAPI,
    DailyLogListCreateAPI,
    DailyLogPhotoAPI,
    ExpenseBatchAPI,
    ExpenseBatchDecisionAPI,
    ExpenseDetailAPI,
    ExpenseListCreateAPI,
    ProjectApproveAPI,
    ProjectAttachmentAPI,
    ProjectAttachmentDetailAPI,
    ProjectCancelAPI,
    ProjectCompleteAPI,
    ProjectDayAPI,
    ProjectDetailAPI,
    ProjectEstimateAPI,
    ProjectHoldAPI,
    ProjectListCreateAPI,
    ProjectRejectAPI,
    ProjectResumeAPI,
    ProjectSpendSummaryAPI,
    ProjectSubmitAPI,
    ProjectSummaryAPI,
    RevisionApproveAPI,
    RevisionListCreateAPI,
    RevisionRejectAPI,
    RevisionWithdrawAPI,
)

urlpatterns = [
    # --- projects ---------------------------------------------------------
    path("projects/", ProjectListCreateAPI.as_view(), name="construction-project-list"),
    path("projects/<int:pk>/", ProjectDetailAPI.as_view(), name="construction-project-detail"),
    path("projects/<int:pk>/summary/", ProjectSummaryAPI.as_view(), name="construction-project-summary"),
    path("projects/<int:pk>/submit/", ProjectSubmitAPI.as_view(), name="construction-project-submit"),
    path("projects/<int:pk>/approve/", ProjectApproveAPI.as_view(), name="construction-project-approve"),
    path("projects/<int:pk>/reject/", ProjectRejectAPI.as_view(), name="construction-project-reject"),
    path("projects/<int:pk>/hold/", ProjectHoldAPI.as_view(), name="construction-project-hold"),
    path("projects/<int:pk>/resume/", ProjectResumeAPI.as_view(), name="construction-project-resume"),
    path("projects/<int:pk>/complete/", ProjectCompleteAPI.as_view(), name="construction-project-complete"),
    path("projects/<int:pk>/cancel/", ProjectCancelAPI.as_view(), name="construction-project-cancel"),

    path(
        "projects/<int:pk>/estimate/",
        ProjectEstimateAPI.as_view(),
        name="construction-project-estimate",
    ),
    path(
        "projects/<int:pk>/attachments/",
        ProjectAttachmentAPI.as_view(),
        name="construction-project-attachments",
    ),
    path(
        "attachments/<int:pk>/",
        ProjectAttachmentDetailAPI.as_view(),
        name="construction-attachment-detail",
    ),

    # --- the daily loop ---------------------------------------------------
    path("projects/<int:pk>/daily-logs/", DailyLogListCreateAPI.as_view(), name="construction-log-list"),
    path("daily-logs/<int:pk>/", DailyLogDetailAPI.as_view(), name="construction-log-detail"),
    path("daily-logs/<int:pk>/photos/", DailyLogPhotoAPI.as_view(), name="construction-log-photos"),
    path("daily-logs/<int:pk>/photos/<int:photo_id>/", DailyLogPhotoAPI.as_view(), name="construction-log-photo-detail"),
    path("projects/<int:pk>/expenses/", ExpenseListCreateAPI.as_view(), name="construction-expense-list"),
    path("expenses/<int:pk>/", ExpenseDetailAPI.as_view(), name="construction-expense-detail"),
    path(
        "projects/<int:pk>/expense-batches/",
        ExpenseBatchAPI.as_view(),
        name="construction-expense-batches",
    ),
    path(
        "expense-batches/<int:pk>/decide/",
        ExpenseBatchDecisionAPI.as_view(),
        name="construction-expense-batch-decide",
    ),
    path("projects/<int:pk>/day/", ProjectDayAPI.as_view(), name="construction-project-day"),
    path("projects/<int:pk>/spend-summary/", ProjectSpendSummaryAPI.as_view(), name="construction-spend-summary"),

    # --- revisions: more money, more time ---------------------------------
    path("projects/<int:pk>/revisions/", RevisionListCreateAPI.as_view(), name="construction-revision-list"),
    path("revisions/<int:pk>/approve/", RevisionApproveAPI.as_view(), name="construction-revision-approve"),
    path("revisions/<int:pk>/reject/", RevisionRejectAPI.as_view(), name="construction-revision-reject"),
    path("revisions/<int:pk>/withdraw/", RevisionWithdrawAPI.as_view(), name="construction-revision-withdraw"),

    # --- the approver's queue ---------------------------------------------
    path("approvals/", ApprovalQueueAPI.as_view(), name="construction-approvals"),
]
