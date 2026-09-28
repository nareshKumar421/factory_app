# Partner Onboarding

> Django app: `partner_onboarding` · Base URL: `/api/v1/partner-onboarding/`
> Frontend: `/register/customer`, `/register/vendor` (public) and
> `/partners/approvals` (`FactoryFlow/src/modules/partner-onboarding`)
> Came from: SAP Portal (`backend_v1`) — `public/register.html`,
> `public/vendor-register.html`, `public/approvals.html`, the `/api/customers`
> routes in `server.js` (~580–860) and `routes/vendors.js`.

Customer and vendor registration, end to end:

1. **A customer or vendor fills in a public form** — no login. Business details,
   contact, GSTIN / PAN / MSME (and TAN / FSSAI / bank accounts for vendors),
   billing and shipping addresses, and the documents (PAN card, cheque, Aadhaar
   or GST certificate, MSME certificate …). It becomes a **PENDING** registration
   in the company the form was sent to.
2. **A verifier** checks it, corrects what is wrong, and **verifies** it
   (→ VERIFIED) or **rejects** it with a reason.
3. **An approver** sets the SAP master data (card-code prefix, BP group, payment
   terms, sales employee, AR/AP control account, credit limit, main group,
   chain, currency, SAP bank codes for a vendor's accounts) and **creates the
   business partner in that company's SAP** (→ APPROVED), or rejects it.

What it is **not**: a customer/vendor master editor. Once SAP holds the partner,
changes are made in SAP. Nothing here writes to SAP tables directly; the
partner is created through the Service Layer (`SAPClient.create_business_partner`).

## Data

| Model | What one row is |
|--|--|
| `CustomerRegistration` | a customer's registration (the portal's `ZCUST_PORTAL` row) |
| `VendorRegistration` | a vendor's registration (`ZVENDOR_PORTAL`), plus TDS / TAN / FSSAI |
| `PartnerAddress` | one billing or shipping address (SAP `BPAddresses`) |
| `VendorBankAccount` | one of a vendor's bank accounts (SAP `BPBankAccounts`) |
| `RegistrationAttachment` | one document, stored as a file under `MEDIA_ROOT/partner_onboarding/` |
| `RegistrationEvent` | one step (submitted, edited, verified, rejected, approved, SAP created, SAP failed, imported) with who and when |

Addresses, documents and events serve both kinds: each has two nullable foreign
keys, `customer` and `vendor`, and a check constraint that exactly one is set —
one table per concept with real foreign keys, instead of a generic relation.

Every registration belongs to one company; the internal screens show the
company in the `Company-Code` header only (another company's row is a 404), and
the partner is created in that registration's company's SAP.

## Endpoints

| Method | Path | Who |
|--|--|--|
| GET | `public/companies/` | anyone (throttled): `[{code, name}]` of the three SAP companies active here |
| GET | `public/states/?company=` | anyone (throttled): SAP's Indian states for that company, cached 6 h (the last good list while SAP is down) |
| POST | `public/customers/` | anyone (throttled): multipart — `payload` (JSON) + files `pan`, `aadhaar`, `cheque`, `msme`, `other`… |
| POST | `public/vendors/` | anyone (throttled): multipart — `payload` + files `pan`, `cheque`, `gst`, `msme`, `fssai`, `other`… |
| GET | `customers/` `?status=PENDING,VERIFIED ?search= ?limit ?offset` | any customer right |
| GET | `customers/<id>/` | any customer right |
| PATCH | `customers/<id>/` | `can_verify_customer_registrations` |
| POST | `customers/<id>/verify/` `{note?}` | `can_verify_customer_registrations` |
| POST | `customers/<id>/reject/` `{reason}` | verify **or** approve right |
| POST | `customers/<id>/approve/` `{SAP fields…, confirm_duplicate?}` | `can_approve_customer_registrations` |
| GET | `customers/<id>/attachments/<attachment_id>/` | any customer right (streamed, never a `/media/` link) |
| … | `vendors/…` — the same, with the vendor rights; approve also takes `bank_accounts: [{id, sap_bank_code}]` | |

Internal endpoints need login + `Company-Code` + the right. SAP's refusal → 400
with SAP's words (`code: sap_refused`), SAP unreachable → 503, SAP broken → 502.
Workflow refusals carry a `code`: `invalid_state` (400), `possible_duplicate`
(409, with `matches`), `sap_posting_in_progress` (409), `documents_not_sent`.

The detail answers `actions: {can_edit, can_verify, can_reject, can_approve}`
worked out from the status and the caller's rights; the screen follows them.

## Rights and groups

| Right | Lets you |
|--|--|
| `can_view_customer_registrations` / `can_view_vendor_registrations` | see the queue, open a registration and its documents |
| `can_verify_customer_registrations` / `…_vendor_…` | verify, edit (while open) and reject |
| `can_approve_customer_registrations` / `…_vendor_…` | set the SAP fields, create the partner in SAP, reject |

Verifying or approving implies viewing. `setup_partner_onboarding_groups` mints:
*Customer Onboarding - Verifier*, *Customer Onboarding - SAP Approver*,
*Vendor Onboarding - Verifier*, *Vendor Onboarding - SAP Approver*,
*Partner Onboarding - Viewer* (both view rights). It puts nobody in them.

## The public forms (decision D2)

* `AllowAny` with **no authentication** at all, so a stale token in a browser
  cannot turn a submission into a 401.
* Throttled per client address: 10 submissions an hour (every attempt counts),
  120 reads an hour (`throttles.py`). The client address is the right-most
  `X-Forwarded-For` entry — the one our own nginx appended — so a forged prefix
  buys no new allowance. Counters live in Django's default cache (per worker
  unless `CACHES` points at Redis). **Staff are not counted**: a submission
  carrying a valid login token of someone holding a partner-onboarding right
  (they open the form from the queue, often many times a day, and an office
  shares one address) passes the submit throttle. The token is read quietly —
  a missing, expired or forged one is simply the public, still throttled,
  never a 401.
* The state list is SAP's (its codes go on the partner's addresses), cached 6 h;
  when SAP cannot be reached the last list it gave is served (kept 30 days in
  the cache), so a short outage costs nobody the form. A 503 only if SAP has
  never answered since the worker started.
* Files: at most 15 MB each, PDF / JPG / PNG only, checked by their first bytes
  (a renamed file is refused), stored as files under names that say nothing
  about whose they are. Twelve files at most per registration.
* Company: one of `JIVO_OIL`, `JIVO_MART`, `JIVO_BEVERAGES`, and it must exist
  and be active here.
* The portal forms' own rules are enforced on the server too (they were only in
  the browser): required fields, GSTIN / PAN / Udyam formats, GSTIN required
  for B2B customers and for vendors, MSME number + certificate (and type +
  business type for vendors), the required documents, a billing address with
  street, city and state, and a vendor's bank account with a valid IFSC.
* Widths are SAP's: a name over 100 characters, a contact name over 50, an
  address line over 100 is refused on the form rather than cut when posting.

## Creating the partner in SAP

`services/workflow.py` `approve()`, following the SAP write rules:

1. Lock the row; it must be VERIFIED and not already being created.
2. Apply the approver's fields; refuse what SAP would refuse (a vendor bank
   account with no SAP bank code, a card code longer than 15 characters, no
   billing address).
3. If a card code is already reserved and SAP holds it for this partner (an
   earlier answer was lost), **adopt** it — mark APPROVED, create nothing.
4. Ask SAP for partners of the same type with the same GSTIN or PAN
   (`partners_with_tax_ids`): **409** with the matches, unless the approver
   sends `confirm_duplicate: true`.
5. Reserve the card code (`next_card_code`, skipping codes SAP or another
   registration holds) and mark the row in flight — **committed before SAP is
   asked**, so a lost answer leaves a trace and a retry reuses the same code
   (SAP refuses a second partner under one code).
6. Upload the documents to one Attachments2 entry (resumable: each landed file
   is marked). A customer's failure stops the approval; a vendor's is a
   warning, as in the portal. Aadhaar is never sent to SAP.
7. Locked again, build the payload and call `create_business_partner` **last**;
   mark APPROVED with SAP's code.

A failure is recorded (`sap_error`, a `SAP_FAILED` event, the in-flight mark
cleared) after the transaction that tried has unwound; the registration stays
VERIFIED with its reservation and the approver's fields. A creation in flight
for more than 10 minutes is treated as abandoned.

The payload mapping, field by field, is the docstring of
`services/sap_payload.py` (the portal's `createCustomer` / `createVendor`).

## Differences from SAP Portal, on purpose

* Rejection only from PENDING or VERIFIED — the portal could reject a partner it
  had already created in SAP (server.js:819, routes/vendors.js:343).
* Verify / edit need the verify right; the portal let any login do both.
* Approve cannot create two partners (lock, reservation, adoption, duplicate check).
* Lookups use the registration's own company; the portal read Oil's.
* Documents are files, not base64 data URLs in an NCLOB.
* Stored now, lost before: a vendor's shipping addresses, main group, chain and
  sales employee; who verified or rejected a customer and when.
* No invented card codes: when SAP cannot be read, nothing is created.
* A vendor bank account with no SAP bank code stops the approval instead of
  silently disappearing from the partner.
* The first billing address takes the registration's GSTIN when its own is
  blank (the portal did this only for a single address, so a B2B customer's
  required GSTIN could miss SAP entirely).
* Address names are built from the SAP state code (`NAME - HR`) and numbered
  when two would clash; SAP refused the portal's duplicates.
* Remarks longer than 100 characters are kept here and left out of SAP's
  Remarks field with a warning.

## Importing SAP Portal's registrations

Export each table as JSON on a machine that can reach the portal's HANA schema
(the portal's `PORTAL_DB_SCHEMA`, by default the Oil company database):

```sql
SELECT * FROM "JIVO_OIL_HANADB"."ZCUST_PORTAL"   ORDER BY "ID";
SELECT * FROM "JIVO_OIL_HANADB"."ZVENDOR_PORTAL" ORDER BY "ID";
```

Save each result as a JSON **array of objects keyed by column name** (DBeaver:
*Export data → JSON*; keep the NCLOB columns `ATTACHMENTS`, `ALL_BILL_ADDRS`,
`ALL_SHIP_ADDRS`, `BANK_ACCOUNTS` as text). The files hold personal data (names,
PAN, bank accounts, ID scans): keep them on the server and delete them after.

```bash
# production only — not run here
python manage.py import_portal_customers --from-file zcust_portal.json --dry-run --settings=config.live_db_settings
python manage.py import_portal_customers --from-file zcust_portal.json --actor ops@example.com --yes --settings=config.live_db_settings
python manage.py import_portal_vendors   --from-file zvendor_portal.json --dry-run --settings=config.live_db_settings
```

* `--dry-run` reads and checks the whole file and prints what a real run would
  do, per status (it reads the database, writes nothing).
* A real run needs `--yes` and `--actor` (who owns the import), prints the
  target database, and writes in one transaction.
* Idempotent on the portal's `ID` (`legacy_portal_id`): re-running creates
  nothing new. `--update` refreshes rows nobody has acted on in JI.
* `COMPANY` (e.g. `JIVO_OIL_HANADB`) is mapped back to a company code through
  `settings.COMPANY_DB`; an unknown or blank one is reported and skipped unless
  `--default-company JIVO_OIL` (or MART / BEVERAGES).
* A row that cannot be read (no ID, unknown status, no name) stops the real run
  unless `--skip-invalid`.
* Addresses, bank accounts and documents become rows and files; timestamps are
  read as UTC (the portal wrote UTC without a zone); every judgement call is a
  note printed per row and kept on the row's *Imported* event; columns JI has
  no field for (zone, RSM, ASM, language …) are kept in `legacy_fields`.
* The vendor table never stored addresses beyond `BILL_*`, so an imported
  vendor has that one address, shipped to itself, named `<NAME[:25]>-<STATE>`
  as the portal named it.

## Setting it up on a live database

1. `manage.py migrate partner_onboarding` creates the tables and the six rights.
2. `manage.py setup_partner_onboarding_groups` creates the five groups.
3. Put people in the groups (Django admin) — the portal's managers in the
   *Verifier* groups, its SAP adders in the *SAP Approver* groups — and check
   each has an active company membership. Until then the module is invisible.
4. Point the public links at `/register/customer` and `/register/vendor`.
5. Optionally import the portal's registrations (above).
6. Prove one creation per company on the SAP sandbox first
   (`sap_client/docs/sap_portal_port.md`, "Verify on the sandbox").

## Tests

`tests.py` (rights, groups, company context, each endpoint against each right),
`tests_public.py` (the forms: no login, throttle, files, company whitelist,
rules), `tests_workflow.py` (verify / reject / edit / list / documents),
`tests_approve.py` (payloads, duplicates, reservation, refusal, adoption,
documents) and `tests_import.py` (both importers). SAP is faked throughout.

```bash
DEBUG=False python manage.py test partner_onboarding --settings=config.sqlite_test_settings
```

SQLite does not lock rows; the locking and the constraints want a
`config.pgtest_settings` run before landing.
