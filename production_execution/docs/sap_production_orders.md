# SAP production orders

> App: `production_execution` · Base URL: `/api/v1/production-execution/sap-orders/`
> Frontend: `/production/sap-orders` (`FactoryFlow/src/modules/production`)
> Came from: SAP Portal (`backend_v1`) — `production.html`, `issue-production.html`,
> `receipt-production.html`, `close-production.html` and their `/api/sap/*` routes.

The run screens work from production runs; these work on SAP production
orders directly, the way SAP Portal did: see every order with what has been
issued to it and received from it, create one (released straight away if
wanted), release a planned one, issue components, receive the finished
product, and close it.

The existing `sap/orders/` endpoints (released/open orders, read by the run
screens and the procurement report) are unchanged.

## Endpoints

| Method | Path | Right |
|--|--|--|
| GET | `sap-orders/` `?status=P\|R\|L\|C ?search ?limit ?offset` | any of the five below |
| POST | `sap-orders/` `{item_code, planned_quantity, due_date, start_date?, warehouse?, remarks?, release?, lines?, confirm_repeat?}` | `can_create_sap_production_orders` |
| GET | `sap-orders/<doc_entry>/` (lines, issues, receipts, actions taken here) | any |
| POST | `sap-orders/<doc_entry>/release/` · `close/` | `can_release_close_sap_production_orders` |
| POST | `sap-orders/<doc_entry>/issue/` `{lines: [{line_num, quantity, warehouse?, batches?}], posting_date?, remarks?, confirm_repeat?}` | `can_issue_for_sap_production_orders` |
| POST | `sap-orders/<doc_entry>/receipt/` `{quantity, warehouse?, batch_number?, posting_date?, remarks?, confirm_repeat?}` | `can_receive_from_sap_production_orders` |

Refused before SAP is asked: releasing anything but a planned order, closing
or issuing to or receiving against anything but a released one, a line that is
not on the order, batches that don't add up to the quantity on a
batch-managed component, a batch-managed product received without a batch. SAP's
own refusal → 400 with SAP's words; unreachable → 503. An issue or receipt SAP
holds for approval answers `{"pending_approval": true, "draft_entry": N}`.

## Differences from SAP Portal

* **Branch.** The portal stamped branch 2 ("FACTORY") whenever the page sent
  none. Here it is the branch of the order's warehouse (`OWHS.BPLid`), or none.
* **Complete / Reject on a receipt.** The portal set it by updating SAP's
  `IGN1` table directly after posting. JI never writes SAP tables, so a
  receipt from here is SAP's default, Complete. To receive a rejected quantity,
  use the SAP client until the Service Layer is shown to accept the
  transaction type on a new receipt line (a check to make on the sandbox).
* **Close** sends `ProductionOrderStatus: boposClosed`; the portal sent the
  database code `'L'`. Prove it on the sandbox company.
* **Double posts.** SAP has no idempotency key and a production order has no
  reference to look it up by, so the dedupe is here: the same request from the
  same person within two minutes is refused (409 `REPEAT_POST`) unless they
  confirm it. Every accepted action is recorded in `SapProductionOrderAction`
  with who took it, since SAP stamps only the shared service account.
* **Rights are checked by the server.** The portal hid the pages from users
  without the `production` module but its API accepted any login.

## Setting it up on a live database

1. `manage.py migrate production_execution` (0046 creates the action log and
   the five rights).
2. `manage.py setup_production_groups` creates *Production SAP Orders* (all
   five) and *Production SAP Orders Viewer*, leaving the other groups as they
   were. It puts nobody in them.
3. Put the people who used the portal's Production pages in *Production SAP
   Orders*.

## Tests

`production_execution/tests_sap_orders.py` — the reader against a mocked
cursor (bound filters, movement totals, resource and batch flags), each
endpoint's guards and payloads, the repeat guard, the approval-draft answer,
rights and company scoping, and the groups.
