# cash_sap — Cash book → SAP

> Django app: `cash_sap` · Base URL: `/api/v1/cash-sap/` · SAP plumbing: `sap_purchasing` · Queue: `sap_postings`
> Built on branch `feat/fixed-asset-grpo` in **separate mode** (`FA_GRPO_SEPARATE_MODE.md`): no existing file or table is changed.
> Paired frontend module: `FactoryFlow/src/modules/cash-sap/`.

## Overview

The factory's petty cash is recorded in `cash_book`. Its approved payment vouchers travel to head office in **bunches**. Today USER22 then keys each bunch into SAP by hand:

- one **imprest purchase order** per bunch, to the imprest vendor (ORGV000465 in Oil), one line per voucher at quantity 1;
- one **GRPO** per voucher (or per voucher group) against that PO, with the bill photographed onto it;
- head office later raises the **A/P invoice**. Heads with no item, such as fuel, toll and courier, are A/P **service** lines and never touch a PO.

`cash_sap` does that from the bunch's SAP page:

- It **reads** `cash_book` and never changes it.
- It **writes to SAP** only through `sap_postings` handlers.
- It uses `sap_purchasing` for every SAP read and write.

## Key concepts

| Row | Meaning |
|---|---|
| `CashBranchSapRouting` | One row per cash-book branch. Holds the SAP company, mode (`PO_GRPO` / `AP_DRAFT_ONLY`), vendor, SAP branch (`bpl_id`), warehouse, Dim1/3/5, tax codes, A/P drafts on/off, attachment needed, GRPO grouping, series hints and cut-over date. |
| `GLItemMap` | What a cash-book G/L head becomes in one target SAP company: `ITEM` (with its CG item), `SERVICE` or `NEVER`, plus `target_account_code` for another company's chart. |
| `VoucherSapInfo` | The bill's own facts: bill date, G.no. and supplier bill no. This replaces the spec's `CashEntry` fields and is the **only** source of these three facts. |
| `BunchSapState` | The bunch's freeze and its member snapshot. This replaces `CashBunch.sap_locked_at` (see *Drift*). |
| `ImprestBatch` | One SAP PO (bunch × SAP company). Holds the routing snapshot and anchors everything else. |
| `ImprestVoucher` | A voucher's posting **overrides** (company, treatment, item, Dim1, Dim3, note) and the **snapshot** taken at freeze. |
| `ImprestGRPO` | One SAP GRPO. Holds its NumAtCard, attachment entry, line map and status. |
| `ImprestAPDraft` | One SAP A/P invoice draft per batch, `ITEM` or `SERVICE`. |
| `ImprestVoucherBill` | A "posting bill": a photo added on the SAP page, because an APPROVED voucher cannot take a bill in the cash book. |

**Statuses** of every SAP document:
- `PLANNED`
- `QUEUED`
- `IN_APPROVAL`
- `POSTED`
- `FAILED`
- `NOT_NEEDED`

QUEUED, IN_APPROVAL and POSTED are *committed*: the voucher is frozen and its snapshot is final.

## Flows

### B. Bunch → PO → GRPOs

```
GET  bunches/<id>/                plan (rules below), per company group
POST bunches/<id>/post/           freeze -> queue PO (+ SERVICE draft) -> send ONE
POST bunches/<id>/send-next/      browser loop, one SAP write per call, 5-min client timeout
  cash_sap.po        enabled -> drift -> simulate? -> guard -> read-back OPOR/ODRF(22) -> PurchaseOrders
                     -> PO lines mapped by FreeText "JI V<entry>" -> GRPO units queued (user=None)
  cash_sap.grpo      PO posted? -> read-back OPDN/ODRF(20) -> Series -> NumAtCard (SAP + JI) -> upload bills
                     -> PurchaseDeliveryNotes (BaseType 22, BaseLine = po_line_num) -> maybe ITEM A/P draft
  cash_sap.po_close  PurchaseOrders(n)/Close after "Leave out"
```

### C. A/P drafts (`ap_drafts_enabled`; off by default)

- The **SERVICE** draft is queued with the PO.
- The **ITEM** draft is queued once every GRPO is POSTED or NOT_NEEDED and at least one is POSTED.
- Both go to `POST /b1s/v2/Drafts` with `DocObjectCode=oPurchaseInvoices`.
- The A/P handler **re-reads the live switch** on the routing before every send.
- Every draft line carries Dim1, Dim2 **and Dim3** from the routing: SAP refuses a draft line without a budget head (rule 1120009), so it cannot be left blank for head office. A total with paise is sent with `Rounding: tYES` (SAP refuses an A/P total that is not whole rupees, 1120018 / 180023).
- Head office opens the draft in JSAP, checks it (it may still change the budget head) and adds it. Re-check SAP then records the OPCH.
- The draft carries **no bill photo yet**. SAP accepts the draft without one, but refuses the final A/P invoice without an attachment (180021), so head office attaches the bills before adding, as it does today. Uploading the vouchers' bills onto the draft is a planned follow-up.

### Plan rules (`plan.py`, in order)

1. Cash received → SKIP.
2. Not APPROVED → blocking `NOT_APPROVED`.
3. **Frozen** (its document is committed) → the snapshot is shown; nothing is recomputed.
4. Company = the voucher override, else the branch routing's company.
5. Salary-advance head → SKIP. This cannot be overridden.
6. Override SKIP → SKIP.
7. Bill date = `VoucherSapInfo.bill_date`, else the voucher date.
8. No company → `UNROUTED` (blocking).
9. No enabled routing for that company → `NO_ROUTING_FOR_COMPANY`.
10. Bill date before the routing's cut-over → SKIP.
11. Map says NEVER → SKIP.
12. Treatment comes from the override, else the map, else SERVICE (with warning `UNMAPPED_HEAD`). `AP_DRAFT_ONLY` turns ITEM into SERVICE.
13. ITEM needs an item (`NO_ITEM`). A multi-item head gives the warning `ITEM_REVIEW`.
14. Decide whether it would be sent.
15. The account in the target company, else `GL_NOT_IN_TARGET`.
16. Dims, tax and warehouse.
17. G.no. and bill no.
18. ITEM without a bill photo → `NO_BILL`.
19. Future bill date → `FUTURE_DATE`.
20. Date warnings, without `DATE_OLD`.

**Across bunches.** A voucher has one `ImprestVoucher` row. The cash book lets a voucher be taken out of an unsent bunch A and put in bunch B, and posting does not wait for a bunch to be sent. While that row is frozen under A (its PO, GRPO or service draft committed, or it was left out after A's PO), B shows the voucher as UNROUTED with a blocking `IN_OTHER_BUNCH`, and B's freeze, overrides and bills refuse to re-home the row. Otherwise it would be posted to SAP a second time. A row from another bunch that is not frozen still starts clean in B.

**Group-level problems:**
- `ROUTING_CONFLICT`: the header tuples of the resolved routings differ.
- `ROUTING_INCOMPLETE`
- `LIVE_SAP_BLOCKED`
- `SIMULATED_ONLY` (warning)
- `SPANS_MONTHS` (warning)
- `NOT_ON_PO`: a voucher became goods after its PO was raised.
- `NOT_ON_AP_DRAFT`: a voucher became a service line after the batch's SERVICE A/P draft was committed (while drafts are on). Freeze never attaches such a voucher to the batch, because it would then count as frozen and never be sent.
- `CHANGED_SINCE_FREEZE` (warning): a PO or SERVICE draft that is not committed yet (PLANNED or FAILED) whose frozen vouchers differ from the plan: an override, the bill facts, the settings or an accepted change. See *Changes after the freeze*.

**Bunch-level problems:**
- `UNROUTED`
- `IN_OTHER_BUNCH`: one or more vouchers are frozen under another bunch.
- `NOTHING_TO_POST` (info)
- `BUNCH_CHANGED`

### Changes after the freeze

FAILED and PLANNED are not committed, so a voucher on a refused PO or SERVICE draft can still be overridden, given new bill facts, or changed by an accepted change. The handlers build from the freeze snapshot, so:

- **Send-next** compares the snapshot of each uncommitted PO or SERVICE draft with the plan before sending it. If they differ, it freezes that company again (exactly what Post does; no SAP call) and lists the units afresh. If the freeze is refused because the change made a voucher blocking, the unit becomes FAILED with the reason and the next unit is tried.
- **The PO and SERVICE A/P handlers** refuse a stale snapshot (FAILED, "Changed since it was frozen ... Press Post (or Retry)"). This covers a Retry from the sap_postings log, which does not go through send-next.
- GRPOs and the ITEM draft need neither: once the PO is committed its goods vouchers are frozen, and the ITEM draft recomputes its amount from the posted GRPOs.

### Dates (D5)

| Field | Value |
|---|---|
| PO `DocDate` = `TaxDate` | 1st of the earliest ITEM bill month |
| PO `DocDueDate` | The latest ITEM bill date |
| GRPO `DocDate` = `DocDueDate` | The posting date: the bill date, or a later re-date |
| GRPO `TaxDate`, NumAtCard date, Dim2 | Always the bill's own date |

## Markers, rounds, read-back (D7)

- **Markers:**
  - `[JI-CB PO <batch>]`
  - `[JI-CB G<grpo>]`
  - `[JI-CB API <draft>]`
  - `[JI-CB APS <draft>]`
- **Rounds.** After a cancel-and-re-raise the marker gains `-<round>` (`[JI-CB PO 42-2]`), so the cancelled document is never adopted again.
- **Read-back before every write.** Every handler reads back the posted table **and** ODRF, schema-qualified, before it writes. A HANA failure means *wait*, never "not there".
- **Approval drafts:**

  | `WddStatus` | Result |
  |---|---|
  | `W` | IN_APPROVAL; wait |
  | `Y` | `SaveDraftToDocument`, then adopt the document |
  | `N` | FAILED, "Rejected in SAP approval" |
  | anything else | Wait |

## NumAtCard (D12)

- **GRPO:** `"<amount>/<dd-mm-yy>"`.
- **A/P:** `"MON YY/<bunch total>/<amount>"`.
- **At freeze:** allocated in GRPO-id order under the bunch lock. JI-unique per (SAP company, vendor) through a partial unique constraint. A clash gets a `.` suffix, up to 5.
- **At send:** re-checked against SAP (`num_at_card_taken`: posted documents and open drafts).

## Series (D6)

- Every PO, GRPO and A/P draft sends `Series` for its own `DocDate` and branch (`resolve_series_for_branch`).
- When more than one series fits, the routing's `*_series_hint` picks one. With no hint the send is refused with SAP's list.
- Turn it off with `CASH_SAP_SEND_SERIES=False`.
- Simulated sends never send `Series`.

## Drift (replaces the bunch lock)

`cash_book` cannot be locked from here, so:

1. **At the first freeze** the bunch's active members are snapshotted: entry id → amount, G/L, branch and voucher date.
2. **Before every freeze, post, send-next and handler send** the bunch is compared with that snapshot.
3. **On any difference** the bunch stops with `BUNCH_CHANGED` (HTTP 400 `{detail, code, drift}`, or a rejected posting), and nothing more is sent.

Ways out:
- **Reset**, when nothing is in SAP yet.
- **Accept the change** (`POST bunches/<id>/accept-changes/ {note}`):

  | Change | Result |
  |---|---|
  | A voucher was taken out, its own document is not committed | Dropped |
  | A voucher was taken out, its PO is already in SAP | Left out after the PO |
  | A voucher whose GRPO or service draft is in SAP | **Refused**: cancel that document in SAP, then Re-check |
  | A voucher whose GRPO is being sent right now | **Refused**: try again in a minute |
  | A changed voucher | Accepted only while uncommitted |
  | A new voucher | Accepted only while no PO is committed |
  | A new or changed voucher that would land on a committed PO or SERVICE draft, or is frozen under another bunch | **Refused** (`NOT_ON_PO`, `NOT_ON_AP_DRAFT`, `IN_OTHER_BUNCH`) |

  Taken-out vouchers are found by entry, not by the row's `bunch` (which may already name the bunch the voucher moved to). A row whose batch is now another bunch's is left to that bunch.

## Escape hatches (D21)

| Action | Endpoint | Allowed when |
|---|---|---|
| Stop sending | `POST bunches/<id>/stop/ {reason}` | always. It cancels QUEUED/REJECTED postings and marks the rows FAILED. A posting claimed by the worker between the read and the cancel is reported under `sending`, not as an error. |
| Re-check SAP | `POST bunches/<id>/recheck/` | always. A cancelled PO with no GRPOs is raised again (round + 1); a cancelled GRPO becomes FAILED (round + 1, attachment entry kept); an A/P draft added by HO is adopted, a deleted one becomes FAILED. |
| Leave out after the PO | `POST bunches/<id>/vouchers/<entry>/leave-out/ {reason}` | the PO is committed, the voucher's GRPO is not in SAP, and the GRPO is not being sent right now (its waiting postings are row-locked while the voucher is taken off) |
| Re-date a GRPO | `PATCH bunches/<id>/grpos/<g>/ {posting_date?, gate_entry_no?}` | the PO is committed, the GRPO is not; the new date is not in the future, not before the PO's date and not before the bill date |
| Close the PO | `POST bunches/<id>/batches/<b>/close-po/` | the PO is POSTED, a voucher was left out, and every GRPO is done |
| Reset | `DELETE bunches/<id>/` | nothing from the bunch is in SAP (simulated bunches always qualify) |
| Retry | `POST bunches/<id>/send-next/ {include_failed: true}` | also re-checks IN_APPROVAL |

## Flags (`flags.py`; read with `getattr`, settings.py not edited)

| Setting | Default | Effect |
|---|---|---|
| `CASH_SAP_ENABLED` | `DEBUG` | Kill switch. Handlers leave rows QUEUED and wait. |
| `CASH_SAP_SIMULATE` | `DEBUG` | Freeze marks rows `simulated`. Their sends build and store payloads but call **no** SAP. |
| `CASH_SAP_ALLOW_LIVE` | `False` | Allow non-`TEST_` company databases (`sap_purchasing.live_guard`) |
| `CASH_SAP_SEND_SERIES` | `True` | Send `Series` |
| `CASH_SAP_UPPERCASE_COMMENTS` | `False` | Upper-case the GRPO and A/P draft Comments (the markers are case-safe). The **PO's** Comments are always upper case: SAP refuses a PO otherwise (rule 2200012). |

The order of checks is binding: **enabled → drift → JI preconditions → simulate → live guard → SAP**.

**These are Django settings, not environment variables.** `config/settings.py` does not define them on this branch, so putting `CASH_SAP_SIMULATE=False` in `.env` changes **nothing**. Set them in a settings module that does `from .settings import *` (as `config/sap_sandbox_settings.py` does), or add `CASH_SAP_ENABLED = config('CASH_SAP_ENABLED', default=False, cast=bool)`-style lines to `settings.py` when merging.

- With `DEBUG=False` and nothing set (a production server), the feature is **off**: Post answers `CASH_SAP_DISABLED`.
- Going live needs `CASH_SAP_ENABLED=True`, `CASH_SAP_SIMULATE=False` and `CASH_SAP_ALLOW_LIVE=True`, because the live company databases do not start with `TEST_`.

**Testing against TEST SAP:**
- Set `CASH_SAP_SIMULATE=False`.
- Keep `CASH_SAP_ALLOW_LIVE=False`: the `TEST_` databases pass the guard.
- Run `python manage.py run_sap_postings --require-test-sap`.

## Permissions and groups

| Codename | Guards |
|---|---|
| `cash_sap.can_post_cash_to_sap` (on `ImprestBatch`) | post, send-next, stop, re-check, accept changes, close PO, voucher overrides, leave out, re-date, bills, reset |
| `cash_sap.can_manage_cash_sap_settings` (on `CashBranchSapRouting`) | routing and G/L map writes |
| any cash_book right or either right above | reading the plan, options, statuses, routing and map |
| `cash_book.can_manage_cash_book` or post | writing a voucher's bill facts (`vouchers/<e>/info/`) |

**Groups:**
- **Cash Book SAP Poster**: `can_view_cash_book` and `can_post_cash_to_sap`.
- **Cash Book SAP Settings**: `can_view_cash_book` and `can_manage_cash_sap_settings`.

Both are created by migration `0002_seed_and_groups`, without members, and by `python manage.py setup_cash_sap_groups [--list]`. The command only ever adds permissions.

## Endpoints (`api/v1/cash-sap/`, names `cash-sap-<thing>`)

| Method + path | Permission | Response |
|---|---|---|
| GET `options/` | read | lists, flags, `test_targets`, `can_post`, `can_manage_settings` |
| GET `bunches/?ids=1,2` (≤200) | read | `{results: [summary]}` |
| GET / DELETE `bunches/<id>/` | read / post | plan (`can_post` ANDed with the caller's right) |
| POST `bunches/<id>/post/` `{sap_company_code?}` | post | `{sent, remaining, plan}` |
| POST `bunches/<id>/send-next/` `{include_failed, skip_posting_ids}` | post | `{sent, remaining, plan}` |
| POST `bunches/<id>/stop/`, `recheck/`, `accept-changes/` | post | plan (+ `notes`) |
| POST `bunches/<id>/batches/<b>/close-po/` | post | `{sent, remaining, plan}` |
| PATCH `bunches/<id>/vouchers/<entry>/` | post | plan |
| POST `bunches/<id>/vouchers/<entry>/leave-out/` | post | plan + `notes` |
| PATCH `bunches/<id>/grpos/<g>/` | post | plan |
| GET / PATCH `vouchers/<entry>/info/` | read / custodian or post | bill facts + `warnings` |
| POST `vouchers/<entry>/bills/` (multipart `file`) | post | 201 bill |
| DELETE `bills/<id>/` | post | 204 |
| GET `routing/` · PUT/PATCH `routing/<branch>/` | read / settings | rows / row |
| GET/POST `gl-map/` · PATCH/DELETE `gl-map/<pk>/` | read / settings | rows / `{row, warnings}` |
| GET `items/?sap_company_code=&search=` | post or settings | `{results}` |

**Errors:**
- A DRF 400 carries `{detail}`, plus `code` for `CASH_SAP_DISABLED` and `BUNCH_CHANGED`.
- SAP down during re-check gives a 503 through `sap_client.drf`.

## Failure modes

| What happens | Result |
|---|---|
| SAP or HANA down | The unit waits (QUEUED). The worker retries with back-off. |
| A timeout after SAP committed | The marker read-back adopts the document on the next try. |
| SAP refuses (400, closed period, missing Dim2 value, no series) | FAILED with SAP's text. Fix it, re-date or override, then Retry (send-next with `include_failed`) or Post. A PO or SERVICE draft whose vouchers changed is frozen again before it is resent; a Retry from the sap_postings log is refused until then. |
| A GRPO refused only because its PO was refused | Set back to PLANNED when the PO posts, its refused posting cancelled, and a fresh one queued for the worker. |
| Reset, Stop or Leave out races the worker for the same posting | Reset and Leave out answer 400 "being sent to SAP right now"; Stop lists it under `sending`. |
| An approval template holds the document | IN_APPROVAL. Retry re-checks it. |
| The bunch changed in the cash book | `BUNCH_CHANGED`. Reset or accept the change. |
| A rebuild deletes a GRPO row that had a QUEUED posting (before the PO is committed) | The posting is later rejected with "no longer exists". This is harmless. |

## Checked against SAP (9 Oct 2026, read-only)

Read with SELECTs only (a READ ONLY transaction, always rolled back) from JIVO_OIL, JIVO_BEVERAGES and their `TEST_` copies:

- **Columns and fields.** Every column `sap_purchasing` reads exists in all of them, and no mandatory user field (`CUFD.NotNull`) sits on OPOR/POR1/OPDN/PDN1/OPCH/PCH1/ODRF/DRF1.
- **Master data the seeds name exists and is open:** ORGV000465 (Oil) and ORGV000245 (Beverages; the same Arvinder Singh imprest, group STAFF VENDOR), branch 2 FACTORY, warehouses BH-FA/DL-FA/GP-FA, tax codes `IGST@0`/`Exampt` (0 %), Dim1 CANOLA/DRINKS/WATER, Dim2 `MM-YYYY` up to 12-2026, Dim3 Factory/FACT_COM/Del Bkhp, Dim5 HR, every seeded G/L account (postable) and CG item (purchase item, not stock, valid).
- **Series.** One PO and one GRPO series per month (no hint needed). Four A/P series fit branch 2 each month; every imprest A/P since January is the `HR_B` family, hence `ap_series_hint = "HR_B"`.
- **Beverages has the same shape as Oil** (PO on the 1st, a GRPO per voucher into BH-FA with `amount/dd-mm-yy` and `G.no.`, A/P made from the GRPOs, services as service A/P), so its routing is `PO_GRPO` on branch 2 with Dim1 DRINKS or WATER, and its own G/L map: item codes mean different things there (CG0000015 is CO2 GAS in Beverages).
- **SAP's validation procedure** (`SBO_SP_TransactionNotification`), the rules our documents meet:
  - PO Comments upper case (2200012) -- the PO's Comments are always upper-cased;
  - Dim1, Dim2, Dim3 on every expense line of a PO, GRPO and draft (2200xx, 2000xx, 11200xx) -- the A/P service draft now carries Dim3;
  - "Factory"/"FACT_COM" budget needs branch 2 (220010, 200015) -- every routing has branch 2;
  - a GRPO needs an attachment with `U_CHK2 = OK` (200019, 200039) -- the existing attachment writer sets it;
  - CG items go to BH-FA/DL-FA/GP-FA (200029);
  - an A/P item line must come from a GRPO (1120002, 180045) -- the item draft copies GRPO lines;
  - an A/P total must be whole rupees (1120018, 180023) -- a draft with paise is sent with `Rounding: tYES`;
  - an A/P invoice needs an attachment (180021) -- see *A/P drafts*: head office attaches the bills before adding.
- **Approvals.** In the live companies the integration user (B1i) is **not** an originator of any active PO, GRPO or A/P approval template, so the app's documents post straight away. USER22's own POs go through template "USER22 VISHAL" today. In `TEST_JIVO_OIL_HANADB`, B1i **is** an originator of most templates, so on TEST the PO and GRPO may stop IN_APPROVAL until someone approves them in SAP.

## OPEN items and residuals

- **MUST FIX before Beverages can post: the Beverages G/L map has no target accounts.**
  - Migration `0002` seeds the `JIVO_BEVERAGES` rows with `target_account_code` blank.
  - Plan rule 15 reads a blank as "the same code" **only for the cash book's own company** (JIVO_OIL).
  - So every Beverage/Water goods voucher is blocked with `GL_NOT_IN_TARGET`: 40 vouchers on TEST on 10 Oct 2026. Oil is not affected.
  - The same G/L codes exist and are postable in JIVO_BEVERAGES (read-only check, 9 Oct).
  - Either fill each Beverages goods head's target account (the same code) in Cash → SAP settings, or set it in a data migration.
  - The tests did not catch it: their fixture still routes Beverages as `AP_DRAFT_ONLY`.
- **Test data has no bill photos.** On the TEST database (10 Oct 2026) no cash voucher has an attachment, so every goods voucher shows `NO_BILL` and no bunch can post until a posting bill is added on the SAP page.
- **SAP approval of the imprest PO.** Today USER22's imprest POs pass SAP approval template "USER22 VISHAL"; posted by the app (as B1i) they would not, in the live companies. The app's own voucher approval comes first, but accounts must say whether the SAP approval must also apply (then the SAP team adds B1i to that template as an originator).
- **Seeds.** The routing and G/L seeds are now SAP's own pattern (above). Accounts should still confirm the heads marked `needs_review` -- booked both as GRPO goods and as A/P service lines (e.g. REFRESHMENT in Oil: 50 GRPO lines, 48 service lines).
- **Bills on A/P drafts.** Drafts go without the bill photos; head office attaches them before adding (SAP refuses the A/P itself without one). Uploading them onto the draft is a follow-up.
- **Comments on drafts.** It is assumed that a document added from a draft keeps the draft's Comments, and so the marker. `find_document_by_draft_key` is the fallback after `SaveDraftToDocument`.
- **Drift can only be accepted, not prevented.** A voucher can still be taken out of a bunch in `cash_book` while it is being posted. The proper fix is the spec's `CashBunch.sap_locked_at` check in `cash_book.services.remove_from_bunch`, which waits for permission to edit `cash_book`.
- **Postings of deleted GRPO rows are not cancelled** when a rebuild deletes the row (see *Failure modes*).
- **A voucher moved to another bunch after it was frozen cannot be posted from the new bunch.** Its one `ImprestVoucher` row (OneToOne on the entry) keeps the first bunch's PO, GRPO or leave-out history. The new bunch blocks it (`IN_OTHER_BUNCH`) until it is taken out of that bunch, or until the first bunch's document is cancelled in SAP and Re-checked. A voucher left out after the first bunch's PO stays blocked for good; key it into SAP by hand. Lifting this needs one row per (entry, bunch), a schema change.
- **Leave out vs. a GRPO that has never been queued.** Leave-out locks the GRPO's waiting postings, so the worker and a person's Retry cannot start one mid-change. A first `post_now` of a GRPO that has no posting at all creates its row without that lock. If it starts in the few milliseconds between leave-out's check and its commit, the handler can read the old members. A full fix means the GRPO handler takes a lock it does not hold today.
- **The bunch list shows every never-posted bunch as "Not posted".** Building a plan per bunch over the remote TEST database timed out the page (47 bunches, 10 Oct 2026), so `bunches/?ids=` no longer builds plans: a bunch with no batches is `NOT_STARTED` there. Whether anything in it goes to SAP at all ("Nothing goes to SAP") shows on the bunch's own page.
- **The cash book's `today()` UTC bug** in the frontend (`CashEntryDialog.tsx:59`, `AdvancesPage.tsx:45`, `AtmPage.tsx:40`) is not fixed here, because those files exist already.

## Developer file map

| File | Contents |
|---|---|
| `models.py` | The tables above |
| `migrations/0001_initial.py` | Written by hand; depends on `cash_book.0014` |
| `migrations/0002_seed_and_groups.py` | Seeds and groups |
| `constants.py` | Constants, markers |
| `flags.py` | Flags, `require_target` |
| `routing.py` | `routing_for`, `header_tuple`, `home_company_code` |
| `voucher_dates.py` | Date refusals and warnings |
| `drift.py` | Member snapshot comparison |
| `uploads.py` | Posting-bill file checks, mirroring cash_book's rules |
| `plan.py` | `build_plan`, `resolve_voucher`, `group_vouchers`, `bunch_status_summary` |
| `payloads.py` | The exact §2.6 JSON |
| `services.py` | Freeze, post, send-next, escape hatches, settings |
| `sap_posting.py` | The four handlers. `titles.py` holds titles and links. |
| `permissions.py`, `serializers.py`, `views.py`, `urls.py`, `admin.py` | The API and the admin |
| `management/commands/setup_cash_sap_groups.py` | The groups command |
| `support_for_tests.py` | The shared test fixture |
| `tests_*.py`, `test_groups.py` | The tests |

## Tests

The tests are `TestCase` / `SimpleTestCase`. SAP is mocked:
- `cash_sap.sap_posting.PurchasingClient`;
- `sap_purchasing.client.PurchasingClient` (re-check);
- `sap_purchasing.live_guard.CompanyContext` (the guard).

Flags are always set explicitly. Run:

```
python manage.py test cash_sap --settings=config.sqlite_test_settings
```

| Module | Covers |
|---|---|
| `tests_payloads` | §2.6 exact dicts |
| `tests_voucher_dates` | Dates and drift |
| `tests_plan` | Plan rules |
| `tests_freeze` | Freeze, NumAtCard |
| `tests_handlers` | The handlers and the worker |
| `tests_services` | send-next, stop, reset, leave-out, re-date, re-check, accept changes; a voucher moved to another bunch, a GRPO being sent, GRPOs queued again, stale snapshots frozen again, late service lines, cancel races |
| `tests_api` | The HTTP surface |
| `test_groups` | The groups command and migration |
| `tests_seed` | Migration 0002 |

**Run on 10 Oct 2026:** 140 tests, all pass, with `config.sqlite_test_settings` and every outside host pointed at a dead local port. `makemigrations cash_sap --check --dry-run` reports no changes.

**Tried by hand** against the TEST app database in SIMULATE mode: the bunch list, a bunch's plan, and the settings pages. Nothing has been posted to SAP, not even to TEST SAP.
