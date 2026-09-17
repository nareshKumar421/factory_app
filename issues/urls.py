from django.urls import path

from .views import (
    IssueBulkStateAPI,
    IssueCommentDetailAPI,
    IssueCommentListAPI,
    IssueDetailAPI,
    IssueLabelDetailAPI,
    IssueLabelListAPI,
    IssueListAPI,
    IssueMetaAPI,
    IssueStateAPI,
    IssueTimelineAPI,
    IssueUploadAPI,
    SupportContactAPI,
)

urlpatterns = [
    path("meta/", IssueMetaAPI.as_view(), name="issue-meta"),
    # Public: the login screen shows this number to users who cannot sign in.
    path("support-contact/", SupportContactAPI.as_view(), name="support-contact"),
    path("uploads/", IssueUploadAPI.as_view(), name="issue-upload"),
    # Masters, before the <int:number> catch-all so "labels" is never read as
    # an issue number.
    path("labels/", IssueLabelListAPI.as_view(), name="issue-label-list"),
    path("labels/<int:label_id>/", IssueLabelDetailAPI.as_view(), name="issue-label-detail"),
    path("comments/<int:comment_id>/", IssueCommentDetailAPI.as_view(), name="issue-comment-detail"),
    path("bulk-state/", IssueBulkStateAPI.as_view(), name="issue-bulk-state"),
    # The issues themselves, addressed by number.
    path("", IssueListAPI.as_view(), name="issue-list"),
    path("<int:number>/", IssueDetailAPI.as_view(), name="issue-detail"),
    path("<int:number>/timeline/", IssueTimelineAPI.as_view(), name="issue-timeline"),
    path("<int:number>/comments/", IssueCommentListAPI.as_view(), name="issue-comment-list"),
    path("<int:number>/state/", IssueStateAPI.as_view(), name="issue-state"),
]
