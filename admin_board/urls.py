from django.urls import path

from .views import AdminBoardAPI, AdminDispatchBillsAPI

app_name = "admin_board"

urlpatterns = [
    path("board/", AdminBoardAPI.as_view(), name="admin-board"),
    path("dispatch-bills/", AdminDispatchBillsAPI.as_view(), name="admin-board-dispatch-bills"),
]
