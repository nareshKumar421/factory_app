from django.urls import path

from .views import HrBoardAPI

app_name = "hr_board"

urlpatterns = [
    path("board/", HrBoardAPI.as_view(), name="hr-board"),
]
