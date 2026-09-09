# SAP Identities — who each app user is inside SAP

> Django app: `sap_client` · Base URL: `/api/v1/sap-identity/`
> Model: `sap_client.SapApproverIdentity` · Admin page: `/admin/sap-identities`
> Consumer today: the SAP transfer-approval queue —
> [`warehouse/docs/sap_transfer_approvals.md`](../../warehouse/docs/sap_transfer_approvals.md)

## The problem it solves

SAP accepts an approval decision from exactly **one** account — the authorizer
its approval template names on the request's current stage. Anyone else,
superuser included, is refused with `-6006`.

Knowing *which* account that is was never the hard part: SAP records it, and the
app reads it (`OWDD.CurrStep` → `WDD1.UserID` → `OUSR.USER_CODE`). The gap was
the other direction — whether the person clicking **Approve** in this app is
that authorizer. Without an answer the app can only hold a pool of shared
credentials and sign with whichever one fits the row, which makes every
decision anonymous: SAP records `USER37`, our audit records whoever clicked, and
nothing ties the two to the same human. That is the same shape as the OMS A/P
approvals where one account signed 4,900 decisions.

This table closes it. A decision is offered only when the caller's own mapped
SAP account **is** the authorizer, so `USER37` in SAP means the person who is
`USER37`.

## Why it is not derived from the warehouse

The rule SAP applies *is* warehouse-based — the templates' saved queries
(`WTM5` → `OUQR`) test `OWTR` warehouse fields, e.g. Oil template 80 fires when
the destination is `BH-WST` (→ `USER32`) and template 38 when the source is one
of `BH-GJ`/`BH-CRUDE`/`BH-LO`/`BH-VA` (→ `USER06`).

But SAP evaluates those queries when the draft is raised and freezes the result
in `WDD1`. Re-deriving it app-side would mean maintaining a second copy of ~30
saved queries that goes wrong the moment somebody edits one in SAP — and SAP
would still accept only its own answer.

It also cannot be expressed as warehouse → one user: **41% of Oil transfer
drafts this year (1,945 of 4,731) fired two or three templates at once**, each
opening its own request with its own authorizer. A single-user map has nowhere
to put the second one.

So warehouse decides *which template*, SAP resolves *which authorizer*, and this
table only answers *is that you*.

## Model

| Field | Notes |
|--|--|
| `user` | FK to the app user |
| `company` | FK — the mapping is **per company** |
| `sap_user_code` | `OUSR.USER_CODE`, upper-cased on save |
| `sap_user_name` | `OUSR.U_NAME` snapshot, display only, never matched on |
| `is_active` | from `BaseModel`; an inactive row authorizes nothing |

Two unique constraints, both load-bearing:

* `(user, company)` — a person is one SAP account per company.
* `(company, sap_user_code)` — a SAP account belongs to one person, or two
  people could both act as the same authorizer and the audit trail could not
  say which of them decided.

Per **company**, because `USER37` is the same human in Oil, Mart and Beverages
but a separate SAP account in each, with its own password (verified live: the
Beverages password authenticated while Oil refused the same string with `-304`).
One person can also hold different codes in different companies.

`SapApproverIdentity.code_for(user, company)` is the lookup the approval
endpoints use. `password_configured` reports whether
`SAP_APPROVER_CREDENTIALS[company][code]` is set — the link says who may decide,
that says whether the decision can be signed.

## Passwords are not stored here

They stay in the `SAP_APPROVER_CREDENTIALS` env map, keyed by the same SAP user
code. The table can therefore be edited by an administrator through the app
while the secrets stay out of Postgres, and the API only ever returns the
boolean. Nothing under `/sap-identity/` returns a password.

## Endpoints

| Method | Path | Permission |
|--|--|--|
| `GET`/`POST` | `identities/` | `sap_client.can_manage_sap_identities` |
| `PATCH`/`DELETE` | `identities/<pk>/` | same |
| `GET` | `sap-users/` | same — `?include_locked=1` to show locked accounts |
| `GET` | `me/` | **none beyond login** |

`company` is never taken from a request body; it comes from the company context,
so an administrator cannot map an identity into a company they are not acting in.

`me/` deliberately needs no permission: every approver screen asks what the app
will sign as on the caller's behalf, and a screen cannot correctly disable an
action it is not allowed to ask about (same reasoning as
`warehouse/my-warehouses/`).

`sap-users/` reads `OUSR` so a code cannot be mistyped into a mapping that
silently never matches. Each row also carries `authorizing_templates` — distinct
**active** templates naming that user across ObjType 13, 67 and 1250000001 — plus
`password_configured` and `mapped_to`. Those three together are the collection
worklist: an account that authorizes something, is mapped to nobody, and has no
password on file is work outstanding, and the admin page summarises exactly that.

## Deployment

1. `migrate sap_client` (0001) and `migrate warehouse` (0019).
2. Grant `sap_client.can_manage_sap_identities` to whoever administers this.
   Note the module-nav consequence: the permission pulls the **Admin** module
   into that user's sidebar.
3. Map the authorizers per company on `/admin/sap-identities`.
4. Add each mapped account's password to `SAP_APPROVER_CREDENTIALS`.

Until 3 and 4 are done for a given company, its SAP approval queue is read-only
— every row lists, none is actionable. That is the intended default.

## Tests

`sap_client/tests_identity.py` — the constraints, per-company scoping, the
context-not-body company rule, the permission split between `identities/` and
`me/`, and that no endpoint leaks a password. All DB-backed; the HANA read
behind the picker is mocked.
