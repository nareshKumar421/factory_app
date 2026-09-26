# BOM Changes

> Django app: `bom_changes` · Base URL: `/api/v1/bom-changes/`
> Frontend: `/bom-changes/*` (`FactoryFlow/src/modules/bom-changes`)
> Came from: SAP Portal (`backend_v1`) — `/api/bom-requests` in `server.js`
> (lines 20–60 and 293–535), `services/bomRequestStore.js` (the `ZBOM_REQUESTS`
> table), `/api/sap/bom/*` in `routes/sap.js` (lines 799–839), and the screens
> `public/bom.html` and the BOM part of `public/approvals.html`.

A bill of materials in SAP (`ProductTrees`: `OITT` + `ITT1`) is changed here in
two ways:

* **A new BOM** (`CREATE`) for an item that has none — a `ProductTrees` POST.
* **A change to a BOM** (`UPDATE`) — a PUT that replaces the whole tree, so a
  line left out of the request disappears from SAP too.

Either is raised as a request, approved level by level, and written to SAP by
the last approval. Someone with the direct right can write one to SAP at once.
The **SAP BOMs** viewer reads the trees SAP already holds (search, then one tree
with its items and resources).

What it is **not**: `warehouse.BOMRequest` / `/api/v1/warehouse/bom-requests/`,
which is production asking the store for the material a BOM calls for. Nothing
here issues material. `production_execution`, `packing_material` and
`planning_purchase` read BOMs for their own sums; this app is the only one that
writes them.

## The approval ladder

`settings.BOM_CHANGE_APPROVAL_LEVELS` (env `BOM_CHANGE_APPROVAL_LEVELS`, default
3, only 2, 3 or 4 accepted — the portal's `BOM_APPROVAL_LEVELS`) is the number
of sign-offs, the last of which writes SAP.

| Levels | PENDING | L1_APPROVED | L2_APPROVED | L3_APPROVED |
|--|--|--|--|--|
| 2 | level 1 | **push** → SAP | — | — |
| 3 | level 1 | level 2 | **push** → SAP | — |
| 4 | level 1 | level 2 | push | **push** → SAP |

* With **2** levels the level-2 right has no step: the push follows level 1
  (the portal's `{PENDING: manager, L1_APPROVED: sap_adder}`).
* With **4** levels the push right signs twice, and the same-person rule makes
  those two different people (the portal's "SAP Adder L1" and "SAP Adder L2").
* **One person approves a request once** (server.js lines 454–458), so nobody
  signs two of its levels. It is also a database constraint.
* **Whoever may approve at a status may reject there** — and the same-person
  rule applies to rejecting too, as it did in the portal.
* **Cancel**: only a `PENDING` request, only by the person who raised it or
  someone holding the push or direct right (server.js lines 526–529).
* If the setting is lowered while requests are in flight, a request already
  past the new last step is treated as at the last step, so it can still be
  pushed.

Statuses keep the portal's values: `PENDING`, `L1_APPROVED`, `L2_APPROVED`,
`L3_APPROVED`, `SAP_PUSHED`, `REJECTED`, `CANCELLED`. (The portal listed `DRAFT`
but never wrote it.)

## Rights and groups

| Right | Portal role | Lets you |
|--|--|--|
| `can_view_bom_changes` | — | see requests and the SAP BOM viewer |
| `can_request_bom_changes` | any login | raise a request |
| `can_approve_bom_level_1` | `manager` | approve / reject at level 1 |
| `can_approve_bom_level_2` | `sr_manager` | approve / reject at level 2 |
| `can_push_bom_to_sap` | `sap_adder` | the last approval, which writes SAP; cancel anyone's pending request |
| `can_push_bom_directly` | `admin` | write a new or changed BOM to SAP at once, with no approvals |

Every right implies viewing. `setup_bom_changes_groups` mints *BOM Changes -
Viewer*, *Requester*, *Level 1 Approver*, *Level 2 Approver*, *SAP Pusher* and
*Admin* (every right). Each role group can also raise requests, as every portal
role could.

## Endpoints

| Method | Path | Right |
|--|--|--|
| GET | `workflow/` — levels and who signs each | any BOM right |
| GET | `sap-boms/?search=&limit=` — trees by code, description or item name | any BOM right |
| GET | `sap-boms/<tree_code>/` — one tree, every line | any BOM right |
| GET | `requests/?status=A,B&kind=&mine=true&actionable=true&search=&limit=` | any BOM right |
| POST | `requests/` — raise a request | `can_request_bom_changes` |
| POST | `requests/direct-push/` — raise and write to SAP now | `can_push_bom_directly` |
| GET | `requests/<id>/` — lines, decisions, progress, SAP result | any BOM right |
| POST | `requests/<id>/approve/` `{remarks}` | the right the current status needs |
| POST | `requests/<id>/reject/` `{remarks}` | the right the current status needs |
| POST | `requests/<id>/cancel/` | submitter, or push / direct right |

Each request row carries `can_approve`, `can_reject`, `can_cancel` and
`can_push` (approving it now writes SAP) for the caller; the page's buttons
follow them. The list also answers `counts` per status and `ACTIONABLE` (the
caller's turn). SAP's refusal → 400 with SAP's words, SAP unreachable → 503
(a write that timed out says to check SAP before trying again), SAP broken →
502, a new BOM SAP already has → 409.

A request body:

```json
{"kind": "CREATE", "item_code": "FG0000121", "item_name": "CANOLA OIL 1 LTR 20 PCS",
 "quantity": "20", "bom_type": "Production", "warehouse": "BH-PF",
 "distribution_rule": "", "project": "",
 "lines": [{"item_type": "item", "item_code": "RM0001", "quantity": "20",
            "issue_method": "Backflush", "warehouse": "BH-PC", "unit_cost": "150.5",
            "comment": ""},
           {"item_type": "resource", "item_code": "JWPL09240001", "quantity": "20"}]}
```

For `UPDATE`, any header field left out is taken from the tree SAP holds; the
tree is kept as `original_data` so approvers can compare.

## The SAP write

Built exactly as the portal's `pushBomToSap` (server.js lines 485–519):
`TreeCode`, `TreeType` (`iProductionTree`/`iSalesTree`/…), `Quantity`,
`ProductDescription` (first 100 characters), `PriceList: -1`, `Warehouse`,
`DistributionRule`, `Project`, and per line `ItemCode`, `Quantity`,
`IssueMethod` (`im_Manual`/`im_Backflush`), `ItemType` (`pit_Item`/`pit_Resource`),
`VisualOrder`, `PriceList: -1`, `Warehouse` (the line's, else the header's),
`Price` + `Currency: INR` when the unit cost is above zero, and `Comment`.

A change (PUT) keeps the portal's differences: the header `Warehouse` is always
sent (`''` when empty), the description only when there is one, each line keeps
its visual order, and **lines carry no price and no comment**.

Order of a push (the final approval, or a direct push):

1. Lock the request row (`select_for_update(of=("self",))`) and re-check its status
   and the caller's right.
2. New BOM: ask HANA whether `OITT` already has a tree for the item → **409** if
   so; if HANA cannot answer, nothing is posted. Change: read the tree SAP holds
   (refused if it is gone) and keep it in `original_data`.
3. Call SAP last. Success → `SAP_PUSHED` with `sap_result`, who and when.
4. SAP refusing → 400 with SAP's words, the status unchanged, the final
   approval not recorded, and the error noted on the row (`push_error`) after
   the transaction has unwound.

A direct push is one transaction: the request, its lines, its level-0 approval
and the SAP call. If SAP refuses, no request is left behind.

## Differences from SAP Portal, on purpose

* **A failed direct push leaves nothing.** The portal inserted a `PENDING` row,
  then pushed, and left the row in everyone's queue when SAP refused (server.js
  lines 316–327, 340–349).
* **SAP is called after the database, never before.** The portal's admin branch
  of create/update pushed first and saved after (lines 366–367, 401–402), so a
  failed save left a BOM in SAP with no record.
* **A new BOM SAP already has is refused (409)** before the POST — and already
  when the request is raised, rather than after three approvals.
* **Closed requests stay closed.** The portal's admin could approve a pushed,
  rejected or cancelled request and push it again; here only an open status can
  be decided.
* **No admin bypass on an existing request.** The portal's admin could approve
  any request straight to SAP, exempt from the same-person rule. Here the direct
  right writes a *new* request straight to SAP; an existing request goes up the
  ladder like any other (an Admin-group member can sign one of its levels).
* **The search covers every tree.** The portal's search read the first 100 trees
  from the Service Layer and filtered those.
* **A change keeps the tree's distribution rule and project.** The portal's
  change form never showed them and its PUT left them out; here they are
  pre-filled from SAP and sent.
* **A line comment over 100 characters is refused**, not cut. The item name is
  kept as SAP has it (the portal upper-cased it).
* **The parent cannot be its own component**, and quantities must be above
  zero — refused before SAP sees them.
* **Rights are checked by the server** at every step; the portal's list and
  detail were open to any login.

## Importing the portal's requests

Export the portal's table as JSON, one object per row keyed by its own column
names. On the portal's HANA (schema `PORTAL_DB_SCHEMA`, else `SAP_B1_COMPANY`):

```sql
SELECT "ID", "TYPE", "ITEM_CODE", "ITEM_NAME", "QTY", "BOM_TYPE", "WAREHOUSE",
       "DISTR_RULE", "PROJECT", "COMPONENTS", "ORIGINAL_DATA", "STATUS",
       "SUBMITTED_BY", "SUBMITTED_NAME", "SUBMITTED_AT", "APPROVAL_LOG",
       "REJECTED_BY", "REJECTED_AT", "SAP_PUSHED_AT", "SAP_PUSHED_BY",
       "SAP_RESULT", "COMPANY"
FROM "<PORTAL_DB_SCHEMA>"."ZBOM_REQUESTS"
ORDER BY "ID";
```

Save the result as a JSON array (or a tool's `{"<query>": [...]}` export), with
the NCLOB columns as text. Then, on the server:

```text
# production only — not run here
python manage.py import_portal_bom_requests --from-file zbom_requests.json --dry-run
python manage.py import_portal_bom_requests --from-file zbom_requests.json --yes
python manage.py import_portal_bom_requests --from-file zbom_requests.json --default-company JIVO_OIL --yes
```

* `--dry-run` checks the whole file without a database and prints every row it
  would skip and why, plus notes on the rows it would import.
* The real run prints the target database and refuses without `--yes`; it is one
  transaction.
* **Idempotent on `legacy_portal_id`** (the portal's `ID`): a row already
  imported is left alone. One whose portal status has moved on since is
  reported, not overwritten.
* `COMPANY` (e.g. `JIVO_OIL_HANADB`) maps to a company code through the inverse
  of `settings.COMPANY_DB`. Rows with an empty `COMPANY` (written before the
  portal added the column) are skipped unless `--default-company` names theirs.
* `COMPONENTS` → lines (the portal's issue-method aliases land on what they were
  pushed as); `APPROVAL_LOG` → decisions (an `admin` entry is a level-0 direct
  push); `ORIGINAL_DATA` and `SAP_RESULT` are kept; timestamps are read as UTC,
  which is what the portal wrote.
* Portal usernames are not JI logins and are never matched by name: they are
  kept as text (`legacy_submitted_by`, `legacy_decided_by`,
  `legacy_sap_pushed_by`). An imported open request joins the queue at the
  level it had reached; its portal approvals do not count toward the
  same-person rule.

## Setting it up on a live database

1. `manage.py migrate bom_changes` creates the three tables and the six rights.
2. `manage.py setup_bom_changes_groups` creates the six groups. It puts nobody
   in them.
3. Put people in the groups, mapping portal roles: `manager` → *Level 1
   Approver*, `sr_manager` → *Level 2 Approver*, `sap_adder` → *SAP Pusher*,
   `admin` → *Admin*, everyone else who raised BOM requests → *Requester*. Until
   then the module is invisible.
4. Optionally set `BOM_CHANGE_APPROVAL_LEVELS` in `.env` to the portal's
   `BOM_APPROVAL_LEVELS` (default 3).
5. Import the portal's requests (above) at the cut-over.

## Verify on the sandbox before relying on it

`config.sap_sandbox_settings` points Oil at the sandbox company:

1. `ProductTrees` POST with the payload above creates the tree; the PUT replaces
   its lines (see `sap_client/docs/sap_portal_port.md`, item 3). Check whether a
   PUT that leaves out line `Price`/`Comment` clears them — the portal's change
   did leave them out.
2. The HANA columns the reader uses (`OITT."Name"`, `"ToWH"`, `"OcrCode"`,
   `"Project"`; `ITT1."IssueMthd"`, `"Price"`, `"Currency"`, `"Comment"`, `"Uom"`)
   are SAP B1's standard names; `Code`, `TreeType`, `Qauntity`, `Father`,
   `ChildNum`, `VisOrder`, `Type`, `Quantity`, `Warehouse`, `ItemName` are
   already read elsewhere in this codebase.

## Tests

`bom_changes/tests.py` (the ladder at 2, 3 and 4 levels, the same-person rule,
reject and cancel, row flags, every endpoint's rights, company scoping, the
CREATE and UPDATE payloads, the 409, a lookup that fails, SAP refusing, the
direct push, the viewer, groups and the permission surface),
`bom_changes/tests_import.py` (the importer) and
`sap_client/tests_bom_reader.py` (the HANA reader). SAP is mocked throughout.
