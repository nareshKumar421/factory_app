from django.urls import path

from .views import (
    CostTypeOptionsAPI,
    FactoryExpenseBoardAPI,
    FactoryExpenseMatrixAPI,
    FactoryExpenseSettingsAPI,
    MonthlyBudgetDetailAPI,
    MonthlyBudgetListCreateAPI,
    ResolvedRatesAPI,
)

urlpatterns = [
    path("board/", FactoryExpenseBoardAPI.as_view(), name="factory-expense-board"),
    # The same spend as a company x bucket grid. Its own endpoint rather than a
    # flag on board/: the two payloads share no shape, and the matrix has to
    # split electricity and salary by ownership, which the board never does.
    path("matrix/", FactoryExpenseMatrixAPI.as_view(), name="factory-expense-matrix"),
    path("settings/", FactoryExpenseSettingsAPI.as_view(), name="factory-expense-settings"),
    # Read-back of the Cost Master rows the board prices with. Rates are
    # created and edited in Admin › Cost Master, never here.
    path("rates/", ResolvedRatesAPI.as_view(), name="factory-expense-rates"),
    # The Cost Master types either tile can be pointed at, for the dropdowns.
    path("cost-types/", CostTypeOptionsAPI.as_view(), name="factory-expense-cost-types"),
    path("budgets/", MonthlyBudgetListCreateAPI.as_view(), name="factory-expense-budgets"),
    path(
        "budgets/<int:pk>/",
        MonthlyBudgetDetailAPI.as_view(),
        name="factory-expense-budget-detail",
    ),
]
