# SAP transfer approvals — Backend

> Django app: `warehouse` · Base URL: `/api/v1/warehouse/sap-transfer-approvals/`
> Frontend: the **SAP approvals** tab on `/warehouse/transfer-requests`
> (`SapTransferApprovalTable.tsx`).

Shows — and decides — the approval requests SAP's own approval procedure is
holding on **inventory transfers** (`ObjType 67`) and **inventory transfer
requests** (`ObjType 1250000001`). This is distinct from the app's own transfer
approvals: most of these were raised straight in the SAP client, and until now
the only place to clear them was SAP itself.

## Why per-user credentials exist

SAP accepts a decision from exactly **one** account: the authorizer its
approval template names on the request's *current* stage
(`OWDD.CurrStep` → `WDD1.UserID` → `OUSR.USER_CODE`). Anyone else — a service
account, even a superuser — is refused with `-6006 "You are not permitted to
perform this action"`. Every stage across Oil, Mart and Beverages has
`OWST.MaxReqr = 1` and a single user in `WST1`, so one shared approval account
cannot cover the estate. Each authorizer's own password is needed.

Hence `SAP_APPROVER_CREDENTIALS`, a JSON env var keyed by company code then SAP
user code:

```
SAP_APPROVER_CREDENTIALS={"JIVO_BEVERAGES": {"USER37": "secret"}, "JIVO_OIL": {}}
```

JSON (not a comma list) so a password may contain any character; env (not the
database) so passwords are never stored in Postgres. `config/settings.py`
upper-cases the user codes on the way in, so lookups are case-insensitive, and
a malformed value raises `ImproperlyConfigured` at startup rather than failing
one decision later. `sap_client/registry.py` hands each company its own slice as
`service_layer["approvers"]`.

The invoice-approval page signs the same way (`invoice_approval/views.py`), so
both SAP approval surfaces now name their authorizer. The legacy single
`SAP_APPROVAL_USER` / `SAP_APPROVAL_PASSWORD` pair survives only as the fallback
`ApprovalRequestWriter` uses when a caller names no `approver`; nothing in the
app relies on it. Leaving the invoice page on that fallback was what made it
sign as `SL_USER` (`B1i`) on production and collect `-6006` on requests whose
authorizer was somebody else.

**Passwords are per company database, not per person.** `USER37` is the same
human in Oil, Mart and Beverages but a separate SAP account in each, with its
own password — verified live: the Beverages password authenticated while Oil
refused the same string with `-304 "Fail to NONE-SSO login from SLD"` (SAP's
generic bad-credentials answer on the Service Layer). So collect one password
per person *per company they authorize in*, and leave a company out of the map
until its password is known rather than guessing: an unconfigured authorizer
degrades to a clear "password not on file" on the row, whereas a wrong one
sends a failing login at SAP on every attempt.

## Endpoints

| Method | Path | Notes |
|--|--|--|
| `GET` | `sap-transfer-approvals/` | `?status=PENDING` (default), `APPROVED`, `REJECTED` or `ALL`; `?limit=` |
| `PATCH` | `sap-transfer-approvals/<wdd_code>/status/` | `{"status": "APPROVED"}` or `{"status": "REJECTED", "rejection_reason": "…"}` |

`wdd_code` is SAP's `OWDD.WddCode`. Permissions are the transfer-request ones:
`warehouse.can_view_transfer_request` to read, `can_approve_transfer_request`
to decide.

Each listed row carries the authorizer plus three derived flags:

* `is_mine` — the caller's own mapped SAP account **is** this authorizer.
* `credentials_configured` — we hold that SAP account's password.
* `can_decide` — `is_mine` **and** configured **and** still pending **and** the
  caller holds `can_approve_transfer_request`.

Rows belonging to other people are still returned. Seeing that a transfer is
stuck, and on whom, is the point; hiding them would just make the transfer
invisible in both systems.

## Who may decide: the identity gate

SAP accepts a decision only from the authorizer it named, so the app offers one
only to the person who *is* that authorizer — resolved through
`sap_client.SapApproverIdentity` (see
[`sap_client/docs/sap_identities.md`](../../sap_client/docs/sap_identities.md),
admin page `/admin/sap-identities`).

Without this, holding a pool of approver passwords would make the app a
rubber stamp: any transfer-approver could sign as `USER37` on a transfer they
never looked at, and SAP's record and our audit row would name different humans.

Deliberately *not* warehouse-scoped. The rule SAP applies is warehouse-based —
the templates' saved queries test `OWTR` warehouse fields — but SAP evaluates
them at draft time and freezes the answer in `WDD1`, and the authorizer it picks
is frequently a manager of neither warehouse. Gating on `UserWarehouse` would
therefore gate the wrong person while still letting them act under someone
else's credentials.

The decision endpoint answers three distinct refusals, each naming its own
cause, because they need three different fixes:

| Situation | Status | Fix |
|--|--|--|
| caller has no mapping in this company | 403 | map them on `/admin/sap-identities` |
| caller is mapped, but to a different account | 403 | the named authorizer decides it |
| caller *is* the authorizer, no password on file | 400 | add it to `SAP_APPROVER_CREDENTIALS` |

## The queue is company-wide, not warehouse-scoped

Unlike the invoice-approval page, this list takes no `whs`. A transfer has two
warehouses with two different managers, and SAP's authorizer is frequently
neither of them — scoping by the caller's warehouses would hide rows nobody
could then find. Warehouse scoping happens on the app's own transfer requests,
which is where it means something.

## Decision flow

1. Re-read the stage from HANA (`transfer_approval_stage`). The page may have
   been open while somebody advanced the request in SAP, and signing as a stale
   stage's authorizer would be refused.
2. Refuse if the request is no longer `PENDING`, if SAP names no authorizer, if
   the caller is not that authorizer, or if its password is not configured —
   each with a sentence naming the user, not a SAP error number.
3. `ApprovalRequestWriter.decide(..., approver=<user code>)` logs a Service
   Layer session in **as** that user and PATCHes
   `ApprovalRequests(WddCode)` with an `ApprovalRequestDecisions` line carrying
   the same credentials.
4. Write a `SapApprovalAudit` row. SAP records only the authorizer, so this is
   the only place that knows which employee actually clicked. A failure here is
   logged and swallowed — SAP has already accepted the decision, and a 500
   would be a lie.

The approver named in the request body is ignored; step 1 is the only source.

## After approval: posting the actual transfer

Approval means different things to the two object types, and conflating them is
how an approved request sits for weeks reserving stock nobody shipped:

| Object | Approving it | Then what |
|--|--|--|
| `67` Inventory Transfer | clears the approval — the document is still a **draft** | somebody must *add* the draft; only then does stock move |
| `1250000001` Transfer Request | clears the request only | one or more transfers must be posted against it |

Neither object moves stock on approval, and the first row is the one that
surprises people: an inventory transfer held by an approval procedure lives in
`ODRF`, not `OWTR`, and approving it leaves it there. **33 approved transfer
drafts were sitting unadded across the three companies** when this was written —
3 in Beverages, 13 in Mart, 17 in Oil — the oldest 612 days old, each one stock
its warehouse believes it has already sent.

An approved `OWTQ` keeps `OpenQty` on its lines until enough `OWTR` documents
reference it (`BaseType 1250000001`, `BaseEntry`, `BaseLine`), at which point it
closes. Partial service is the norm, not the exception: every posted Oil request
this year carries one to three transfers.

`warehouse/services/sap_transfer_post_service.py` is that step, and
`GET sap-transfer-requests/awaiting/` is the backlog it works from — the
**Awaiting transfer** tab beside the approval queue. It is the SAP-raised twin
of `transfer_request_service`: that one posts the app's own requests keyed on a
`WarehouseTransferRequest` row, this one is keyed on the SAP `DocEntry` because
a request raised in the SAP client has no local row.

Rules it enforces, all for reasons SAP will not enforce for you:

* **Quantities are checked against the line's live `OpenQty`**, re-read at post
  time. SAP will happily post a movement bigger than the request reserved, and
  a concurrent transfer may already have taken part of it.
* **A closed line is refused**, naming the item.
* **Zero or omitted is skipped, not an error** — leaving a line for later is the
  normal case, and the request stays open for it.
* **Quantities stay `Decimal` end to end** (`json_safe` handles the wire). Loose
  oil moves in fractions; 143.846 KGS is a real quantity and a float round-trip
  is how a tank ends up 0.001 out.
* **Batches FIFO**, and only for items `OITM.ManBtchNum` marks batch-managed.
* **Only the source warehouse's manager may post**, since posting is what moves
  stock out of it (`warehouse_scope.assert_manages`).

**Same-branch only.** A branch-crossing move needs two legs through an `*-INT`
warehouse, which the app's own flow models with a local record tracking the leg
in between; rather than half-implement that against a document we do not own, a
cross-branch request is refused by name and left to SAP. That costs almost
nothing: of 526 Oil transfer requests raised this year, exactly one crossed
branches.

Both `Transfer Requester` and `Transfer Approver` carry
`can_post_transfer_to_sap`. Posting is not a second approval — it is the act of
moving stock a decision already authorised — and leaving it with the sender
alone stranded approved requests with nobody on the page able to finish them.

## After approval, part two: adding the transfer draft

`warehouse/services/sap_transfer_draft_service.py` is the app's **Add** button,
and `GET sap-transfer-drafts/` is the backlog it works from — shown above the
requests in the same **Awaiting transfer** tab, because to a warehouse both are
the same wait: approved, and the stock has not moved.

| Method | Path | Notes |
|--|--|--|
| `GET` | `sap-transfer-drafts/` | approved, unadded transfer drafts; `?limit=` (default 100) |
| `POST` | `sap-transfer-drafts/<draft_entry>/post/` | no body — the draft is added exactly as SAP holds it |

`draft_entry` is `ODRF.DocEntry`. Reading takes `can_view_transfer_request`;
adding takes `can_post_transfer_to_sap`, the same permission as posting against
a request, plus management of every warehouse the stock leaves.

How it differs from posting against a request, and why:

* **Nothing is chosen.** The request flow builds a document from open
  quantities, so it takes a quantity per line. This one posts a document SAP
  already holds — items, quantities, warehouses and the operator's own batch
  allocations. Editing belongs on the draft, in SAP.
* **Batches are never re-allocated.** Unlike an A/R invoice draft, a transfer
  draft *does* carry its allocations (`DRF16`, keyed `AbsEntry`/`LineNum`):
  every batch-managed line of all 33 waiting drafts had them. FIFO-allocating
  here would silently move batches other than the ones chosen. A line that is
  batch-managed with no allocation is flagged on the row instead, since SAP
  would refuse the add with `-4014`.
* **Cross-branch is fine.** A branch-crossing *request* would have to be built
  as two legs and is refused; a draft already says what it is, in-transit leg
  and all — `BH-VG` → `DL-INT` is one of the waiting ones.
* **Only approved drafts are listed.** A draft SAP never routed for approval
  (`WddStatus = '-'`, 7 of them) is just as unadded, but it is also where a
  half-keyed document sits; adding one from here would post work its author had
  not finished.
* **The already-added check runs before the state checks.** Adding a draft
  closes it, so an already-added one would otherwise be refused as "closed"
  when what the operator needs is the number of the transfer that exists.
* **A timeout is resolved, not reported.** The add runs the full document post;
  on a timeout the service re-reads `OWTR."draftKey"` and reports success if SAP
  committed it, because a retry would move the stock twice.

Mechanically the add is `POST /b1s/v2/DraftsService_SaveDraftToDocument` with
`{"Document": {"DocEntry": N, "DocObjectCode": "oStockTransfer"}}` — the same
call the A/R invoice flow makes with `oInvoices`. SAP answers `204` with no
body, so the posted document is read back through `OWTR."draftKey"`.

Every attempt, successful or not, writes a `SapTransferDraftPost` row. SAP
records the add against the Service Layer account, so that row is the only place
that knows which employee pressed the button. Failures are recorded too:
`SBO_SP_TransactionNotification` runs on the add and never ran at draft time, so
a draft that saved cleanly months ago can be refused today for a reason nobody
sees twice.

## Data facts worth not rediscovering

* `OWDD.DraftEntry` — not `DocEntry` — is the FK to `ODRF.DocEntry`.
* On a transfer draft header the source warehouse is `ODRF.Filler`; the
  destination is `ODRF.ToWhsCode`. There is no `FromWhsCode` column.
* On the **lines** (`DRF1`) the sense is reversed from a sales line:
  `FromWhsCod` is the source and `WhsCode` the destination. `source_stock`
  joins `OITW` on `FromWhsCod`, because what an approver needs to know is
  whether the *sending* warehouse holds the quantity.
* **An added draft is not deleted — it is closed.** SAP sets `DocStatus = 'C'`
  and `WddStatus = '-'` on it, and the posted `OWTR` points back through
  `OWTR."draftKey"`. So a still-to-add draft is `DocStatus = 'O'`, and the
  approved ones carry `WddStatus = 'Y'` (2,166 closed against 3 open in
  Beverages). A draft's `DocNum` is provisional but SAP keeps it on the add.
* Editing a draft cancels its request and opens a new one, so stale `OWDD` rows
  keep `Status = 'W'` while their draft says `WddStatus = 'C'`/`'N'`. Only the
  latest request per draft is live, and PENDING further requires the draft to
  say `WddStatus = 'W'` and `DocStatus = 'O'`. **Without that filter Oil reports
  292 pending transfers where only 4 are real.**

## Related: why the app's own postings never appear here

`B1i` (`SL_USER`) is an originator (`WTM1`) only on templates that are
`Active = 'N'` in Oil and Beverages, so a transfer the app posts itself raises
no approval request at all in those two companies — it just posts. That is the
real cause of the "Service Layer bypasses approval procedures" behaviour: the
originator list, not the Service Layer. Mart is the exception, where `B1i` is an
active originator on templates 6, 15, 17, 18, 23, 36, 39 and 44.

## Tests

`warehouse/tests_sap_transfer_draft.py` — the add: every state SAP would refuse
(pending, rejected, cancelled, closed, another object type), the already-added
guard, a timeout SAP actually committed, the per-source warehouse scoping, and
that a refusal comes back as SAP's own words.

`warehouse/tests_sap_approval.py` — the credential resolution (no database
needed), and the API behaviour including all three refusals, that the request
body cannot choose who signs, that a deactivated or other-company mapping does
not count, and that a failed audit write never undoes a decision SAP accepted.
`sap_client/tests_identity.py` covers the mapping itself. SAP is mocked throughout —
nothing reaches HANA or the Service Layer.
