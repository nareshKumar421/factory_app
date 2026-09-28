# SAP Portal merged into JI

SAP Portal (`backend_v1`, a Node/Express app with static HTML pages, run under
pm2 as `sapportal-jivo`) has been rebuilt inside JI. Both talked to the same
SAP — same HANA host, same Service Layer host and user, same three company
databases — so no SAP data moves. What moved is behaviour (ported, not copied)
and the portal's own four tables, which have importers.

Source ported from: the live server's working tree, committed in the portal
repo as `efa29d8` on branch `snapshot/server-2026-09-26` (it had drifted from the
last commit). The portal's report-path hole is fixed on the same branch as
`7b73fbe` (see "Before anything else").

## Where each portal screen went

| Portal page | JI page | Backend | Rights |
|--|--|--|--|
| Journal Entries, General Ledger, Chart of Accounts | `/sap-finance/journal-entries`, `/general-ledger`, `/chart-of-accounts` | `sap_finance` | `sap_finance.can_view_sap_ledgers` |
| Budget | `/sap-finance/budgets` | `sap_finance` (SAP `BUDGET` UDO) | `can_view_sap_budgets`, `can_manage_sap_budgets` |
| Documents (+ attachments, payment drafts) | `/sap-documents` | `sap_documents` | `sap_documents.can_view_sap_documents`, `can_download_sap_attachments` |
| SAP Approvals | `/sap-approvals` | `sap_approvals` | `can_view_sap_approval_inbox`, `can_decide_sap_approvals`, `can_withdraw_own_sap_approvals` |
| Credit Notes | `/warehouse/credit-note-approval` (existing), plus "Without Qty Posting" and withdraw | `warehouse` | the existing A/R and A/P credit-note rights |
| BOM (requests + approvals) | `/bom-changes/sap-boms`, `/bom-changes/requests` | `bom_changes` | `bom_changes.*` (view, request, level 1, level 2, push, direct push) |
| Customer / Vendor registration | public `/register/customer`, `/register/vendor`; queue `/partners/approvals` | `partner_onboarding` | view / verify / approve × customers and vendors |
| Production, Issue, Receipt, Close | `/production/sap-orders` | `production_execution` (`sap-orders/`) | five `production_execution.*_sap_production_orders` rights |
| Reports | `/sap-reports` (existing) | `sap_reports` | existing |
| Admin (users, roles, modules) | Django admin + `/admin/sap-identities` | `accounts`, `sap_client` | existing |
| Master-data pickers (`/api/sap/lookup/*`) | used by the forms above | `sap_client` at `/api/v1/sap-lookups/` | login + company |

Each app's `docs/README.md` has its endpoints, rules and differences from the
portal. The SAP plumbing is described in `sap_client/docs/sap_portal_port.md`,
the production-order screens in `production_execution/docs/sap_production_orders.md`.

## Before anything else: the portal's report-path hole

Until the portal is retired, deploy its fix. The reports endpoints took a folder
from `?path=`, so any logged-in portal user could read files and run shell
commands on the server that also hosts JI.

1. Push the portal branch `snapshot/server-2026-09-26` (commits `efa29d8`,
   `7b73fbe`) to the portal's remote, or copy `routes/sap.js` and
   `tests/report-files.test.js` from it.
2. On the server, in `/home/superadmin/backend_v1`: bring in `routes/sap.js`,
   run `node --test tests/report-files.test.js`, then `pm2 restart sapportal-jivo`.
3. Check whether the portal's seed logins (`admin`, `manager1`, `srmanager1`)
   still have their seeded passwords; disable or reset them.

## Setting it up on production

In order; each step is safe to repeat.

1. **Deploy the backend** (`/deploy`). New migrations: `sap_finance 0001`,
   `sap_documents 0001`, `sap_approvals 0001`, `bom_changes 0001`,
   `partner_onboarding 0001` and `production_execution 0048` create tables;
   `accounts 0008` adds `User.must_change_password` with a database default, so
   the previous release still runs on the migrated schema.
2. **Server `.env`** (nothing here is required to boot; see DEPLOYMENT.md):
   - `SAP_FILE_SERVICE_BASE_URL` = the portal's `FILE_SERVICE_BASE` value, to switch
     attachment downloads on. Company ids default to Oil 1, Beverages 2, Mart 3.
   - `BOM_CHANGE_APPROVAL_LEVELS=3` (the portal's live value; 2–4 allowed).
3. **Groups.** Each command creates groups and puts nobody in them:
   ```
   manage.py setup_sap_finance_groups
   manage.py setup_sap_documents_groups
   manage.py setup_sap_approvals_groups
   manage.py setup_bom_changes_groups
   manage.py setup_partner_onboarding_groups
   manage.py setup_production_groups        # adds "Production SAP Orders" (+ Viewer); others unchanged
   manage.py seed_local_sap_reports         # the Inventory Audit report, in every company
   ```
4. **Deploy the frontend.**
5. **Prove the SAP writes on the sandbox** before anyone uses them live
   (`--settings=config.sap_sandbox_settings`: real SAP hosts, `JIVO_OIL` → the
   sandbox company `TEST_JIVO_OIL_HANADB`, the other two companies refused). The
   checklist is below.
6. **Import the portal's data.** Export each table as a JSON array (the SELECTs
   are in the commands' help and the apps' READMEs; leave out
   `ZCUST_USERS.PASSWORD`). Dry run, read the report, then write:
   ```
   manage.py import_portal_users        --from-file zcust_users.json    --read-sap --dry-run   # then --yes
   manage.py import_portal_customers    --from-file zcust_portal.json   --dry-run              # then --actor <email> --yes
   manage.py import_portal_vendors      --from-file zvendor_portal.json --dry-run
   manage.py import_portal_bom_requests --from-file zbom_requests.json  --dry-run              # then --yes
   ```
   Users are matched by email and created without a usable password. JI sends
   no email, so give them one: `manage.py issue_temporary_passwords
   --without-password --output <new file>` writes a temporary password per user
   to a file only its owner can read (or use the Django admin action for a few);
   hand each over, delete the file, and JI makes each user choose their own at
   first login. Portal users with no usable email are skipped — add them by
   hand. Portal roles and modules become the groups above, and each portal SAP
   user id becomes a `SapApproverIdentity` per company. Review the report's
   "unrestricted" users (portal admins, `sap_adder`s, users with no module
   list) by hand, or rerun with `--grant-unrestricted`. Then:
   - give each user the companies they work in (`UserCompany`) — the portal let
     everyone act on every company, JI shows one company at a time;
   - assign the Inventory Audit report on `/admin/sap-report-access` to each
     user who had the portal's `reports` module: in JI no assignment means no
     reports.
7. **Approvers.** The general inbox and the credit-note queue let approvers
   type their own SAP password for each decision or withdrawal (never stored);
   the transfer queue still needs `SAP_APPROVER_CREDENTIALS`. Either way each
   approver must be mapped on `/admin/sap-identities`; the users importer does
   most of it.
8. **Run both, then cut over** (merge plan phases 6–7). Pause new work in the
   portal, run the importers once more (idempotent; `--update` for partners
   refreshes rows nobody has touched in JI), point the portal's pages at the JI
   routes, and keep the portal read-only for 2–4 weeks. Then `pm2 delete
   sapportal-jivo`, remove its nginx route and port 5002, and revoke the portal
   HANA account's write rights. Leave the four `Z*` tables in SAP's schema until
   sign-off; dropping them is a change to SAP's database.

## Verify on the sandbox

Nothing below was possible without SAP. Every other path is covered by tests
with SAP mocked.

- `BusinessPartners` create with the payload `partner_onboarding` builds (UDF
  values `U_MSME_Type` upper-case, `U_Fssai`, bank accounts, `GstType`).
- `ProductTrees` create and PUT (does a PUT without line Price/Comment clear them?).
- `BUDGET` create, PATCH with `B1S-ReplaceCollectionsOnPatch`, and delete.
- Production order close sends `boposClosed` (the portal sent `'L'`); release,
  issue for production and receipt from production.
- Approval withdraw with `Status: arsCancelled`; a decision signed with a typed password.
- The credit-note "Without Qty Posting" draft-line PATCH.
- Service Layer filters in the document browser: `contains(CardCode, …)`,
  `Cancelled eq 'tYES'`, listing `PaymentDrafts`.
- HANA columns that no other JI code read before: `OITT` Name/ToWH/OcrCode/
  Project/PriceList/UpdateDate, `ITT1` IssueMthd/Price/Currency/Comment, `OPDF`
  columns, `OWST` stage names, `OJDT.CreatedBy`, `TransId`/`ObjType`/`draftKey`
  on document headers, `ATC1.Line`, `OACP.LinkAct_24`.
- Whether the Service Layer accepts a transaction type (Complete/Reject) on a
  new receipt-from-production line. The portal set it with a direct `IGN1`
  update, which JI does not do.
- The attachment file service, by entry and by name.

## What changed on purpose

- **Rights are enforced by the server.** The portal's `modules` list only hid
  sidebar links; its API accepted any login for almost everything.
- **Every lookup reads the caller's company** (the portal read Oil for partner
  lookups). **Values are bound, never formatted into SQL**, and OData keys are quoted.
- **No direct SAP table writes.** The receipt Complete/Reject `IGN1` update is gone.
- **Duplicate protection** where the portal had none:
  - business partners: row lock, GSTIN/PAN check, card code reserved before posting;
  - BOMs: existing-tree check;
  - production orders: a create, issue or receipt is claimed before SAP is asked
    (one in flight per payload), and one SAP did not answer is not sent again
    until the operator has checked SAP and confirms;
  - approvals and credit notes: draft-aware stale guards, and an approval over a
    credit note SAP already posted is refused unless confirmed.
- **Workflow bugs fixed:** an approved registration can no longer be rejected;
  verify and edit need the verify right; a closed BOM request cannot be re-pushed.
- **Approval visibility is strict identity** (`SapApproverIdentity`), not the
  portal's name matching.
- **Approvers see what they sign on their queue's own right:** the approvals
  inbox and the credit-note queue show the draft in full (lines, TDS, journal
  preview, base documents) and its attachments, limited to that request's files.
- **A customer whose documents cannot be sent to SAP is not created** (the
  portal created it anyway); a vendor's documents are still a warning.
- **Imported users get a temporary password** (`issue_temporary_passwords`) and
  must choose their own at first login; JI sends no email.
- **`/api/v1/po/grpo/` needs the GRPO posting right** (it checked only login
  and company).
- **The ledger's running balance is right for any date range.**
- **Not ported:**
  - direct GRPO without a PO (decision D4);
  - the smbclient fallback for attachments;
  - the portal's customer/vendor admin-approve and direct-approve (admin create
    without verification);
  - the BOM admin's approve-anything bypass (direct push creates a new request instead);
  - the unused 3-level customer flow;
  - the portal's report file-share listing (use `sap_reports`);
  - Service Layer fallbacks when HANA is down.

## Decisions taken

The code cites the merge plan's decisions by number; the plan itself was never
saved, so they are recorded here.

- **D1** — the general approvals inbox (and the credit-note queue) let an
  approver type their own SAP password for a decision; it is never stored.
- **D2** — the registration forms stay public, with no login; submissions are
  throttled, except for staff working the queue.
- **D3** — portal users become JI users matched by email, created without a
  password; roles and modules become groups; the portal's SAP user id becomes
  a `SapApproverIdentity` per company.
- **D4** — direct GRPO without a PO is not merged (still open, below).

## Open business decisions

- GRPO without a PO (D4): not available in JI; decide once its use is measured.
- Re-deciding an approval that is already decided: the portal allowed it, JI refuses.
- Receiving a rejected quantity from production: the portal wrote SAP's `IGN1`
  directly; JI does not, so rejects go through the SAP client unless the
  Service Layer proves to accept a transaction type on the receipt line.
- BOM: the portal admin could approve any request straight to SAP; JI keeps the ladder.
- Customer approval levels: live was 2 (verify, approve); the portal's 3-level
  screen had no backend behind it.

## Rollback

- Code: `factory_deploy.sh rollback` / `factoryflow_deploy.sh rollback`. The new
  migrations only add tables and one column with a database default, so the
  previous release runs on the migrated schema.
- Until cutover the portal keeps running. Moving a group of users back means
  removing their JI group and sending them to the portal page.
- The merge branch was rebased onto `origin/main` at `ba6bf26` (backend) and
  `292360ae` (frontend) on 2026-09-28, and is rebased again when it lands: the
  commit before the first SAP Portal commit on main is the state to return to.
  The local tag `pre-sap-portal-merge` marks where the work started (`978845d`,
  `e7301c66`).
- Anything already posted to SAP stays in SAP; a code rollback does not undo it.
