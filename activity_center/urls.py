from django.urls import path

from .views import (
    ActivityDefinitionsView,
    AllUsersActivityView,
    AllUsersDailyBoardView,
    MyActivitySummaryView,
    MyCompletedActivitiesView,
    MyDailySheetView,
    MyPendingActivitiesView,
    UserActivityDetailView,
)

urlpatterns = [
    path("me/summary/", MyActivitySummaryView.as_view(), name="activity-my-summary"),
    path("me/pending/", MyPendingActivitiesView.as_view(), name="activity-my-pending"),
    path("me/completed/", MyCompletedActivitiesView.as_view(), name="activity-my-completed"),
    path("me/today/", MyDailySheetView.as_view(), name="activity-my-today"),
    path("definitions/", ActivityDefinitionsView.as_view(), name="activity-definitions"),
    path("users/", AllUsersActivityView.as_view(), name="activity-users"),
    # Above the <int:user_id> route so the literal segment always wins.
    path("users/today/", AllUsersDailyBoardView.as_view(), name="activity-users-today"),
    path("users/<int:user_id>/", UserActivityDetailView.as_view(), name="activity-user-detail"),
]
