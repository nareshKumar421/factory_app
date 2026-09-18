from django.urls import path

from .views import AccountsBoardAPI

app_name = "accounts_board"

urlpatterns = [
    path("board/", AccountsBoardAPI.as_view(), name="accounts-board"),
]
