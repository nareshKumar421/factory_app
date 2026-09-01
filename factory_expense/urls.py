from django.urls import path

from .views import (
    FactoryExpenseBoardAPI,
    FactoryExpenseSettingsAPI,
    MonthlyBudgetDetailAPI,
    MonthlyBudgetListCreateAPI,
    ResolvedRatesAPI,
)

urlpatterns = [
    path("board/", FactoryExpenseBoardAPI.as_view(), name="factory-expense-board"),
    path("settings/", FactoryExpenseSettingsAPI.as_view(), name="factory-expense-settings"),
    # Read-back of the Cost Master rows the board prices with. Rates are
    # created and edited in Admin › Cost Master, never here.
    path("rates/", ResolvedRatesAPI.as_view(), name="factory-expense-rates"),
    path("budgets/", MonthlyBudgetListCreateAPI.as_view(), name="factory-expense-budgets"),
    path(
        "budgets/<int:pk>/",
        MonthlyBudgetDetailAPI.as_view(),
        name="factory-expense-budget-detail",
    ),
]
