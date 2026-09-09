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

## Data facts worth not rediscovering

* `OWDD.DraftEntry` — not `DocEntry` — is the FK to `ODRF.DocEntry`.
* On a transfer draft header the source warehouse is `ODRF.Filler`; the
  destination is `ODRF.ToWhsCode`. There is no `FromWhsCode` column.
* On the **lines** (`DRF1`) the sense is reversed from a sales line:
  `FromWhsCod` is the source and `WhsCode` the destination. `source_stock`
  joins `OITW` on `FromWhsCod`, because what an approver needs to know is
  whether the *sending* warehouse holds the quantity.
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

`warehouse/tests_sap_approval.py` — the credential resolution (no database
needed), and the API behaviour including all three refusals, that the request
body cannot choose who signs, that a deactivated or other-company mapping does
not count, and that a failed audit write never undoes a decision SAP accepted.
`sap_client/tests_identity.py` covers the mapping itself. SAP is mocked throughout —
nothing reaches HANA or the Service Layer.
