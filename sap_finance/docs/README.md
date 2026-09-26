# SAP Finance

> Django app: `sap_finance` · Base URL: `/api/v1/sap-finance/`
> Frontend: `/sap-finance/*` (`FactoryFlow/src/modules/sap-finance`)
> Came from: SAP Portal (`backend_v1`) — `journal-entries.html`, `gl.html`,
> `chart-of-accounts.html`, `budget.html` and their `/api/sap/*` routes.

The finance screens SAP Portal offered, rebuilt inside JI:

* **Journal entries** — `OJDT` headers with their `JDT1` lines, filtered by
  entry number, reference, SAP type and posting date.
* **General ledger** — every posting to one G/L account, or to one business
  partner, with the balance after each posting and the offsetting account.
* **Chart of accounts** — the `OACT` tree in SAP's ten drawers, title accounts
  rolled up from the postable accounts beneath them.
* **Budgets** — SAP's `BUDGET` user-defined object: a budget head (cost
  dimension 3), an optional sub-budget (dimension 4) and one line per month
  with a fixed and a variable amount. Created, edited and deleted in SAP.

What it is **not**: `budget_approvals` (a read-only dashboard of draft lines
against budget heads) or `cash_book` (the factory's own petty cash). Nothing
here posts a journal entry.

The ledgers are read live from HANA; nothing is cached or stored. The only
table is `SapBudgetChange`: one row per budget change made from here, written
after SAP accepted it, because SAP stamps a UDO write with the shared service
account rather than the person.

## Endpoints

| Method | Path | Right |
|--|--|--|
| GET | `journal-entries/` `?trans_id ?number ?reference ?trans_type ?date_from ?date_to ?limit` | `can_view_sap_ledgers` |
| GET | `general-ledger/?account=` `?date_from ?date_to ?limit` | `can_view_sap_ledgers` |
| GET | `ledger-accounts/?search=` (G/L accounts and partners) | `can_view_sap_ledgers` |
| GET | `chart-of-accounts/` `?search ?drawer` | `can_view_sap_ledgers` |
| GET / POST | `budgets/` | view: `can_view_sap_budgets`; create: `can_manage_sap_budgets` |
| GET / PUT / DELETE | `budgets/<doc_entry>/` | same split |
| GET | `budget-changes/` `?doc_entry` | `can_view_sap_budgets` |

`can_manage_sap_budgets` implies viewing. SAP's refusal → 400 with SAP's words,
SAP unreachable → 503, SAP broken → 502.

A budget body:

```json
{"budget": "BUD-ADMIN", "sub_budget": "",
 "lines": [{"month": "2026-04-01", "fixed_amount": "1000.00",
            "variable_amount": "250.00", "sub_budget": ""}]}
```

Each month may appear once; `PUT` replaces every line (SAP's
`B1S-ReplaceCollectionsOnPatch`), so a line left out is deleted in SAP.

## Differences from SAP Portal

* **The ledger's running balance is right for any date range.** The portal
  walked backwards from today's balance even when the range ended in the past,
  so every balance shown for such a range was off by what was posted since.
* **Budgets are validated before SAP sees them.** The portal forwarded the
  page's body to SAP as it came.
* **Rights are enforced by the server.** The portal gated only journal entries
  on the server; the ledger, chart and budget screens were hidden in the
  sidebar but open to any login.

## Setting it up on a live database

1. `manage.py migrate sap_finance` creates the table and the three permission rows.
2. `manage.py setup_sap_finance_groups` creates the groups — *SAP Finance -
   Ledger Viewer*, *SAP Finance - Budget Viewer*, *SAP Finance - Budget
   Editor*. It puts nobody in them.
3. Add the users who need it to a group. Portal users who held the
   `journal-entries` module go in *Ledger Viewer*; `budget`, in *Budget Editor*.
   Until then the module is invisible.

## Tests

`sap_finance/tests.py` (the permission stack, each right, company scoping, the
budget payload and audit, the group command) and `FinanceReaderTests` in
`sap_client/tests_sap_portal.py` (the roll-up, the ledger's anchor, bound
filters). SAP is mocked throughout.
