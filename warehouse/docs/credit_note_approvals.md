# SAP credit-note approvals — Backend

> Django app: `warehouse` · Base URL: `/api/v1/warehouse/credit-note-approvals/`
> Frontend: `/warehouse/credit-note-approval`
> (`src/modules/warehouse/credit-note-approval/`).

Shows — and decides — the approval requests SAP's own approval procedure is
holding on **credit notes**: `ObjType 14` (A/R, a customer is credited) and
`ObjType 19` (A/P, a vendor is debited). Every one of them was raised in the SAP
client, and until now the only place to clear them was SAP itself, so a credit
note waiting on somebody who does not sit in SAP simply stalled with nobody able
to see that it had.

This is the same machinery as [`sap_transfer_approvals.md`](./sap_transfer_approvals.md),
pointed at a different document family. The identity rules, the
`SAP_APPROVER_CREDENTIALS` map and the `-6006` reasoning are all documented
there and are not repeated here; they are enforced for both queues by the one
shared base class, `warehouse/views_sap_approval_base.py`.

## What is different about a credit note

**It is not always goods.** `ODRF.DocType` is `'I'` for an item credit note and
`'S'` for a service one, and 591 of Oil's 1,587 credit-note drafts are service.
A service line has no `ItemCode`, no `WhsCode` and `Quantity = 0` — what it
carries is a G/L account (`DRF1.AcctCode`) and an amount. The reader returns
both shapes and flags which (`line_type`, `moves_stock`), and the page renders
each row from what the document actually holds. Filtering the queue to item
credit notes would strand a third of it back in SAP.

**Stock moves in opposite directions.** Approving an *item* A/R credit note
brings goods back INTO the line's warehouse; an item A/P credit note sends them
back OUT to the vendor (`stock_direction`). The "short" warning on a line is
therefore raised only for the OUT case — on an inbound credit note a low balance
is expected and flagging it would cry wolf on every row.

**The queue is company-wide, not warehouse-scoped.** The authorizer SAP names is
rarely a warehouse manager, and a service credit note names no warehouse at all,
so scoping by warehouse would hide exactly the rows nobody can currently find.

## Data facts (verified live against all three company databases)

* `OWDD.DraftEntry` — not `DocEntry` — is the FK to `ODRF.DocEntry`.
* Editing a draft cancels its request and opens a new one, so stale `OWDD` rows
  with `Status = 'W'` point at drafts whose `WddStatus` is `'C'`/`'N'`. Only the
  LATEST request per draft and template is live (next bullet), and PENDING
  further requires the draft to
  say `WddStatus = 'W'` and `DocStatus = 'O'`. Without that filter Oil reports
  88 "pending" A/R credit notes where 25 are real.
* **"Latest" is per TEMPLATE, not per draft.** A draft that matches two
  approval templates opens one request per template (`OWDD.WtmCode`), both
  waiting at the same time, each on its own authorizer — Oil draft 57272 waits
  on USER26 (template 73, stage 6) *and* USER30 (template 106, stage 19). 11 of
  Oil's 26 pending A/R credit-note drafts are like that. Such a draft shows as
  two rows and that is correct: deciding one does not release the other, and
  the credit note only leaves the queue once every request is approved. Deduping
  per draft instead hid the lower `WddCode` of every pair — which is how a
  credit note "waiting on USER26" according to SAP was listed as waiting on
  USER30 and was invisible to the only person who could sign it.
* **A draft's `DocNum` is not the number the document ends up with.** It is the
  series' next number as at the save, so open drafts share it (three Oil
  credit-note drafts carry 626042613 between them) and the add takes whatever is
  next *then*. The link that holds is the draft entry — `ORIN."draftKey"` /
  `ORPC."draftKey"` — which is how `posted_doc_num` is resolved, and it is the
  only number worth quoting to an operator. Measured live, approved drafts do
  diverge (draft 626085059 → posted 626085066).
* An approved credit note whose draft was never *added* has no posted document
  at all (`posted_doc_num` null). That backlog exists here as it does for
  transfers, and the page names it; adding it is a SAP-client job.
* What a credit note was raised against lives on the LINES
  (`DRF1.BaseType`/`BaseRef`), not the header — invoice (13), return (16), A/P
  invoice (18), GRPO (20) — and a standalone one has none.

## Endpoints

| Method | Path | Notes |
|--|--|--|
| `GET` | `credit-note-approvals/` | `?status=PENDING` (default), `APPROVED`, `REJECTED`, `ALL`; `?family=AR\|AP\|ALL`; `?limit=` (clamped 1–500) |
| `GET` | `credit-note-approvals/pending-count/` | `{"total": n}` — the sidebar badge |
| `PATCH` | `credit-note-approvals/<wdd_code>/status/` | `{"status": "APPROVED"}` or `{"status": "REJECTED", "rejection_reason": "…"}` |

`<wdd_code>` is `OWDD.WddCode`. Each listed row carries `approver_code` (the SAP
user the request waits on) plus `is_mine`, `credentials_configured` and
`can_decide`, so a row the caller cannot act on is still listed with the reason.

## Permissions

Scoped **per family**, because A/R (sales) and A/P (purchasing) are different
jobs — in SAP the two queues' authorizers do not overlap by a single account
(A/P waits on BHAWANI and SHOAIB; A/R on ten other people).

| Permission | Guards |
|--|--|
| `warehouse.can_view_ar_credit_note_approval` | Reading A/R rows |
| `warehouse.can_approve_ar_credit_note` | Deciding an A/R credit note |
| `warehouse.can_view_ap_credit_note_approval` | Reading A/P rows |
| `warehouse.can_approve_ap_credit_note` | Deciding an A/P credit note |

Declared on `CreditNoteApprovalAudit` (`0029` created one pair for both; `0030`
split it and deletes the superseded pair, but only if nobody holds it).

The split is **enforced, not cosmetic**, in three places — a UI filter alone
would be a hole:

* the **list** narrows `family` to what the caller holds. Asking for `ALL`
  returns only their families; asking for a family they lack returns an empty
  list rather than widening to everything.
* the **pending count** narrows the same way, so the badge cannot leak the
  existence of rows the user may not read.
* the **decision** reads the document's family from SAP (`OWDD.ObjType`), not
  from the request body, and refuses 403 if the caller lacks that family's
  approve permission. The endpoint's own `CanApproveCreditNote` gate only
  proves they may decide *something*.

Either view permission opens the page; the queue is then filtered. And as ever,
the permission is **necessary but not sufficient**: the caller must also be
mapped to the SAP account SAP named (`SapApproverIdentity`, Admin → SAP
Identities) with that account's password in `SAP_APPROVER_CREDENTIALS`.
Granting a group alone is safe — such a user reads their queue and decides
nothing.

## Audit

`CreditNoteApprovalAudit` (`warehouse_credit_note_approval_audit`) records one
row per decision taken through the app: the SAP authorizer it was signed as
(`sap_approver`) beside the real employee who clicked (`created_by`), plus the
party and amount. SAP itself only ever knows the authorizer. It is deliberately
a separate table from the transfer queue's `SapApprovalAudit`, which is
route-shaped and has nowhere to put a customer or a total.

A failure to write the audit row **never** undoes a decision SAP has already
accepted; it is logged and the response still succeeds.

## Deploying

1. `python manage.py migrate warehouse` — `0029` (audit table) and `0030` (the
   four family-scoped permissions).
2. `python manage.py setup_credit_note_approval_groups` — creates "Credit Note
   A/R Approver", "Credit Note A/R Viewer", "Credit Note A/P Approver" and
   "Credit Note A/P Viewer"; `--list` shows what they hold. Grant both pairs to
   anyone who genuinely works both sides.
3. Assign the groups, and for anyone who must *decide*: map their
   `SapApproverIdentity` per company and add that SAP account's password to
   `SAP_APPROVER_CREDENTIALS`. Passwords are per company database even for the
   same person — see the transfer doc.

## Related docs

- **The same machinery on transfers:** [`sap_transfer_approvals.md`](./sap_transfer_approvals.md)
  — read this one for `SAP_APPROVER_CREDENTIALS`, the `-6006` rule and why the
  authorizer is re-read from HANA at decision time.
- **Invoice approvals (separate `invoice_approval` app):** the A/R invoice
  equivalent, which is warehouse-scoped where this one is company-wide.
- **Reader:** `sap_client/hana/credit_note_approval_reader.py` ·
  **Views:** `warehouse/views_credit_note_approval.py` ·
  **Tests:** `warehouse/tests_credit_note_approval.py`.
