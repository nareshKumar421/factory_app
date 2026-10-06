from django.urls import path

from .views import (
    AmountsBoardAPI,
    AmountsDebtorBillsAPI,
    AmountsDebtorsAPI,
    AmountsGodownItemsAPI,
    StockOwnersAPI,
)

app_name = "amounts_board"

urlpatterns = [
    path("board/", AmountsBoardAPI.as_view(), name="amounts-board"),
    path("godown-items/", AmountsGodownItemsAPI.as_view(), name="amounts-board-godown-items"),
    path("debtors/", AmountsDebtorsAPI.as_view(), name="amounts-board-debtors"),
    path("debtor-bills/", AmountsDebtorBillsAPI.as_view(), name="amounts-board-debtor-bills"),
    path("owners/", StockOwnersAPI.as_view(), name="amounts-board-owners"),
]
