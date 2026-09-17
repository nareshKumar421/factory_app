from django.urls import path

from .views import AdminBoardAPI

app_name = "admin_board"

urlpatterns = [
    path("board/", AdminBoardAPI.as_view(), name="admin-board"),
]
