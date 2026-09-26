# SAP Approvals

> Django app: `sap_approvals` · Base URL: `/api/v1/sap-approvals/`
> Frontend: `/sap-approvals` (`FactoryFlow/src/modules/sap-approvals`)
> Came from: SAP Portal (`backend_v1`) — `public/sap-approvals.html`,
> `routes/sap.js` `/approval-requests*` (~2780–3013) and `services/sapApprovals.js`.

The general SAP approvals inbox: every approval request SAP holds, of **every
document type**, that involves the person looking — the ones they raised, and
the ones with a decision line of theirs. From here they approve or reject
(signed as their own SAP account) and withdraw what they raised.

What it is **not**: the warehouse queues. Invoice, transfer and credit-note
approvals (`warehouse/`, `invoice_approval/`) each serve one document family
and list it **company-wide**, so a stuck document is visible to the people who
chase it. This inbox spans every family and lists only what involves the
caller. Both read the same SAP data; the queues keep their own behaviour.

Requests are read live from HANA (`sap_client/hana/approval_inbox_reader.py`)
and decided through the Service Layer (`ApprovalRequestWriter`). The only table
is `SapApprovalDecision`: one row per approve / reject / withdraw SAP accepted
from here, naming the app user (`created_by`), the SAP account it was signed as,
whether a password was typed for it and whether a posted-duplicate warning was
overridden. It never holds a password.

## Endpoints

| Method | Path | Right |
|--|--|--|
| GET | `requests/` `?scope=waiting_on_me\|raised_by_me\|all` `?status=PENDING\|APPROVED\|REJECTED\|GENERATED\|CANCELLED\|ALL` `?object_type ?date_from ?date_to ?search ?limit` | `can_view_sap_approval_inbox` |
| GET | `requests/<wdd_code>/` | `can_view_sap_approval_inbox` |
| POST | `requests/<wdd_code>/decision/` `{approve, remarks, sap_password?, confirm_duplicate?}` | `can_decide_sap_approvals` |
| POST | `requests/<wdd_code>/withdraw/` `{sap_password?}` | `can_withdraw_own_sap_approvals` |
| GET | `pending-count/` → `{"total": n}` (waiting on me) | `can_view_sap_approval_inbox` |

Deciding and withdrawing each imply viewing. Every view: login, the
`Company-Code` context, then its right. SAP's refusal → 400 with SAP's words,
SAP unreachable → 503, SAP broken → 502.

`<wdd_code>` is `OWDD.WddCode`. The list answers
`{results, count, limit, truncated, identity, object_types}`; `identity` is
`{sap_user_code, credentials_configured}` or `null` with a `message` when the
caller is not mapped in this company (not an error: the page explains it).

Each row: the request (object type and label, status, stale/superseded flags,
current stage, template, created at, remarks), the people (originator, the
authorizer the current stage waits on, who decided and when, the rejection
reason), the document as the draft holds it (number, party, reference, total,
currency, date, comments), siblings (`request_count`, `pending_request_count`,
`sibling_requests`) and duplicates (`posted_duplicates`, `is_duplicate`,
`already_posted_as`), plus the caller's flags: `is_mine` (pending at a stage of
theirs), `is_originator`, `can_decide`, `can_withdraw` and
`credentials_configured` (a stored password exists for their account, so no
password needs typing). The page shows buttons from these flags only.

The detail adds `stages` (every `WDD1` line: step, stage name, user, status,
remarks, decided at, `is_current`) and the draft's `lines` (item or account,
quantity, price, total, tax code, warehouse, what it was copied from, Without
Qty Posting). Payment drafts (`OPDF`) show no lines (`lines_available: false`).

### Who sees what

Strictly by `SapApproverIdentity`: the caller's mapped SAP user code in this
company, resolved to `OUSR.USERID`. The visibility rule is the portal's —
you raised it, or you have a decision line on it, and while it is pending that
line must be undecided and at the current stage. The detail refuses (403) a
request outside that rule.

### Deciding

Guards, in this order, each before SAP is called:

1. **Still pending** by the draft-aware rule below, else 409 `STALE_REQUEST`
   (a leftover says what happened to its draft; a decided request says so).
2. **Mapped** in this company, else 403; **an authorizer of the current stage**
   (any user still undecided there), else 403 naming who it waits on.
3. **Not a posted duplicate** when approving, else 409 `DUPLICATE_DOCUMENT`
   with `duplicate_of`, unless `confirm_duplicate: true`. Rejecting is never
   blocked — that is how a leftover gets cleared.
4. **Something to sign with**: the password typed now, or the account's stored
   one in `SAP_APPROVER_CREDENTIALS`, else 400.

Then `SAPClient.decide_approval_request(..., approver=<the caller's code>,
password=<typed or None>)`, with remarks naming the app user ("… — approved by
Honey Singh (Factory app)"), then the audit row. The duplicate read that gates
an approval fails closed (502); on a list the same read is decoration and fails
soft.

Withdrawing: pending (409 `STALE_REQUEST`), mapped (403), the originator —
`OWDD.OwnerID` → `OUSR` — (403), something to sign with (400), then
`SAPClient.withdraw_approval_request` as the originator (`arsCancelled`, the
value the portal proved live).

### The typed SAP password (decision D1)

The approver may type their own SAP password for a decision or withdraw. It is
used for that one Service Layer login and call and is **never stored, cached,
logged, echoed in an error or returned**: the serializer never renders it, the
views mark it sensitive for Django's error reports, the audit keeps only
`typed_password: true`, and the tests assert it reaches `SAPClient` and nowhere
else. A typed password never lets anyone act as someone else — the identity
guard runs first. With nothing typed the stored password is used exactly as the
warehouse queues use it.

### When a request is really pending

Two rules together (`approval_inbox_reader.inbox_status` /
`EFFECTIVE_STATUS_SQL`):

1. The draft decides (`sap_client/approval_status.py`, the portal's
   `effectiveOwddStatus`): SAP leaves `OWDD.Status = 'W'` after a draft's
   approval ends, so a request is pending only while its draft (`ODRF`, or
   `OPDF` for payments) says `'W'` too.
2. Only the newest request per (draft, template) is live — JI's queue rule.
   Editing a draft opens a new request while the old header stays at `'W'`
   and the draft is back at `'W'` for the new one; rule 1 alone kept the old
   one pending in the portal. It reads as cancelled here.

## Differences from SAP Portal

* **Identity by `SapApproverIdentity` only.** The portal also matched the
  portal login's user name and display name against the decision lines
  (`sapApprovals.js:83-89`).
* **Superseded requests are not pending** (rule 2 above).
* **Siblings are grouped by (draft, object type)** and counted by the effective
  status. The portal grouped by draft number alone, mixing a document draft
  with a payment draft of the same number, and counted leftovers as open.
* **The duplicate check that gates an approval fails closed.** The portal
  skipped it on a HANA error and approved anyway.
* **Separate rights** for viewing, deciding and withdrawing; the portal had one
  module. The server enforces them.
* **The stored password is a fallback**: the portal always demanded a typed one.
* **Object labels**: 59 is Goods Receipt and 60 Goods Issue (the portal had them
  swapped); `1470000113`, which matches no request, is gone; 24 is labelled.
* **Not ported**: the Service Layer fallback list when HANA is down (the list
  answers 503 instead); the `originatorId` filter (replaced by the "raised by
  me" scope); payment-draft lines, TDS, GL and attachment tabs in the detail;
  a company-wide listing of other people's requests (the warehouse queues are
  that, per family).

## Open business decision

**Re-deciding an already decided request.** The portal let an approver who sat
on a decided request send another decision and left SAP to accept or refuse the
reversal. JI refuses anything that is not pending, in this inbox as in the
warehouse queues. If the business wants reversals, that is a deliberate change
to both, not a port.

## Setting it up on a live database

1. `manage.py migrate sap_approvals` creates the table and the three permission rows.
2. `manage.py setup_sap_approvals_groups` creates *SAP Approvals - Viewer*,
   *SAP Approvals - Requester* (view + withdraw own) and *SAP Approvals -
   Approver* (view + decide + withdraw own). It puts nobody in them.
3. Map each user to their SAP account per company on Admin → SAP Identities.
   Unmapped, the inbox is empty and says why.
4. Optionally store the account's password in `SAP_APPROVER_CREDENTIALS`;
   without it the person types their SAP password for each decision.
5. Add the users to a group. Portal users who held the `sap-approvals` module
   go in *Approver*. Until then the module is invisible.

## Verify before relying on it

The SQL was written against the columns the portal and JI's queue readers
already read live, and is unit-tested against a fake cursor only. Before
relying on it, on the sandbox or a read-only session: the list and badge for a
real approver (compare with SAP's own pendency report), a payment request
(`OPDF` join and columns), the `OWST` stage names, and a posted-duplicate case.

## Tests

`sap_approvals/tests.py` (the permission stack per endpoint, identity scoping,
row flags, the guard order, the typed-password path, withdraw rules, audit,
badge, group command, permission surface) and
`sap_client/tests_approval_inbox.py` (the pending rule with the portal's own
cases, the superseded rule, bound SQL, siblings, the three duplicate shapes,
fail-soft decoration versus the fail-closed gate, stage and line reads, the
Without Qty Posting writer and the signer check). SAP is mocked throughout.
