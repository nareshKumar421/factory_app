from django.urls import path

from . import views

urlpatterns = [
    path("plan/", views.PlanAPI.as_view(), name="tomorrow-run-plan"),
    path("choice/", views.ChoiceAPI.as_view(), name="tomorrow-run-choice"),
    path("rebuild/", views.RebuildAPI.as_view(), name="tomorrow-run-rebuild"),
    path("sheets/", views.SheetListAPI.as_view(), name="tomorrow-run-sheets"),
    path("sheets/<int:pk>/", views.SheetDetailAPI.as_view(), name="tomorrow-run-sheet"),
]
