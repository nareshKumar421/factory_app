from django.urls import path

from .views import (
    BudgetChangeListAPI,
    BudgetDetailAPI,
    BudgetListCreateAPI,
    ChartOfAccountsAPI,
    CustomerAgingAPI,
    GeneralLedgerAPI,
    JournalEntryListAPI,
    LedgerAccountSearchAPI,
    OpenBillsAPI,
    OpenGrpoAPI,
    PartyOutstandingAPI,
)

urlpatterns = [
    path("journal-entries/", JournalEntryListAPI.as_view(), name="sap-finance-journal-entries"),
    path("chart-of-accounts/", ChartOfAccountsAPI.as_view(), name="sap-finance-chart-of-accounts"),
    path("general-ledger/", GeneralLedgerAPI.as_view(), name="sap-finance-general-ledger"),
    path("ledger-accounts/", LedgerAccountSearchAPI.as_view(), name="sap-finance-ledger-accounts"),
    path("budgets/", BudgetListCreateAPI.as_view(), name="sap-finance-budgets"),
    path("budgets/<int:doc_entry>/", BudgetDetailAPI.as_view(), name="sap-finance-budget-detail"),
    path("budget-changes/", BudgetChangeListAPI.as_view(), name="sap-finance-budget-changes"),
    path("outstanding/parties/", PartyOutstandingAPI.as_view(), name="sap-finance-party-outstanding"),
    path("outstanding/bills/", OpenBillsAPI.as_view(), name="sap-finance-open-bills"),
    path("outstanding/grpos/", OpenGrpoAPI.as_view(), name="sap-finance-open-grpos"),
    path("outstanding/aging/", CustomerAgingAPI.as_view(), name="sap-finance-customer-aging"),
]
