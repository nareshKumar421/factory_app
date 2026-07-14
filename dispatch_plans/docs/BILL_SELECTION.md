# Dispatch — Bill Selection (before Plan)

A step **before** the Dispatch Plan page: planners choose which bills enter
dispatch planning. Only selected bills appear on the Plan page for vehicle
linking.

## Behaviour (as agreed)
- **Company-wide (shared) selection** — one selection per company; any planner's
  Submit updates what everyone sees on the Plan page.
- **Submit reconciles only the shown bills** — checked → selected, shown-but-
  unchecked → deselected; bills outside the current date window are untouched, so
  submitting one window never wipes another.

## Data model
`SelectedDispatchBill(company, sap_invoice_doc_entry, sap_invoice_doc_num, is_active, created_by)`
— keyed on `(company, sap_invoice_doc_entry)` like `DispatchPlan`. `is_active=True`
means selected; deselecting flips it off (keeps who/when for audit).

## API
- `POST /dispatch-plans/bills/selection/` — body
  `{ "shown_doc_entries": [int], "selected_doc_entries": [int] }` (the latter must
  be a subset of the former). Returns `{ "selected": n, "deselected": n }`.
  Permission: `dispatch_plans.can_select_dispatch_bills`.
- `GET /dispatch-plans/bills/?selected_only=true` — the Plan page passes this so
  only selected bills return. Every bill row now also carries `is_selected`.

## Permission + auto-assignment
- New model permission `dispatch_plans.can_select_dispatch_bills`.
- Data migration `0018` grants it to **every group** already holding
  `can_view_dispatch_plans` or `can_link_dispatch_vehicle`, **and** to any user who
  holds those directly — so all existing dispatch users get the new page with no
  manual setup (mirrors migration `0006`).

## Frontend
- New route/page `/dispatch/bill-selection` (before `/dispatch/plans` in routes and
  the sidebar), gated by `SELECT_BILLS`. Date-filtered bill table with checkboxes +
  select-all + Submit.
- The Plan page (`DispatchPlansDashboardPage`) requests `selected_only: true`.

## Rollout note
When this ships, the Plan page shows **only selected bills** — so on day one, until
planners select bills, the Plan page is empty for a given window. That is the
intended behaviour per the requirement. If a temporary "show all" escape is wanted,
add a toggle that clears `selected_only` on the Plan page.
