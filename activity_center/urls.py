from django.urls import path

from .views import (
    ActivityDefinitionsView,
    AllUsersActivityView,
    MyActivitySummaryView,
    MyCompletedActivitiesView,
    MyPendingActivitiesView,
    UserActivityDetailView,
)

urlpatterns = [
    path("me/summary/", MyActivitySummaryView.as_view(), name="activity-my-summary"),
    path("me/pending/", MyPendingActivitiesView.as_view(), name="activity-my-pending"),
    path("me/completed/", MyCompletedActivitiesView.as_view(), name="activity-my-completed"),
    path("definitions/", ActivityDefinitionsView.as_view(), name="activity-definitions"),
    path("users/", AllUsersActivityView.as_view(), name="activity-users"),
    path("users/<int:user_id>/", UserActivityDetailView.as_view(), name="activity-user-detail"),
]
