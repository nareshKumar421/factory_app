from django.urls import path

from .views import AmountsBoardAPI, AmountsGodownItemsAPI, StockOwnersAPI

app_name = "amounts_board"

urlpatterns = [
    path("board/", AmountsBoardAPI.as_view(), name="amounts-board"),
    path("godown-items/", AmountsGodownItemsAPI.as_view(), name="amounts-board-godown-items"),
    path("owners/", StockOwnersAPI.as_view(), name="amounts-board-owners"),
]
