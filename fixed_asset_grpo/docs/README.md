# fixed_asset_grpo

Posts a vendor's fixed-asset delivery to SAP as a GRPO, from a fixed-asset gate entry. The SAP PO is linked **after** the gate, on this app's screen. Built on branch `feat/fixed-asset-grpo`.

This app replaces the spec's units U4 and U5 (`FA_GRPO_BUILD_SPEC.md` §2.4 and §2.5). `FA_GRPO_SEPARATE_MODE.md` §2.3 moved all of it into this new app, so `fixed_asset_gatein`, `grpo` and `raw_material_gatein` are **not edited**.

- **The gate wizard is unchanged.** The gate records an arrival with free text (supplier name, bill number, asset lines, invoice photo) and completes it. That locks the entry.
- **The PO is linked later.** On the Fixed Asset GRPO screen, someone with the link or post right attaches SAP PO lines to the completed entry.
- **The GRPO is the engine's own.** `grpo.services.GRPOService` posts it, scoped to `entry_type="FIXED_ASSET"`, through this app's own SAP posting queue kind.

## Contents

- [Flow](#flow)
- [What it stores](#what-it-stores)
- [Endpoints](#endpoints)
- [Flags](#flags)
- [Permissions and group](#permissions-and-group)
- [The queue handler](#the-queue-handler)
- [Simulate: how it works and why](#simulate-how-it-works-and-why)
- [Attachments](#attachments)
- [One warehouse per GRPO](#one-warehouse-per-grpo)
- [Residuals (need an edit to an existing app)](#residuals-need-an-edit-to-an-existing-app)
- [OPEN items](#open-items)
- [Tests](#tests)
- [Registration (lead only)](#registration-lead-only)

## Flow

```
Gate (fixed_asset_gatein, unchanged)
  vehicle/driver -> supplier (free text) + asset lines -> invoice photo -> complete (COMPLETED + locked)

Fixed Asset GRPO (this app; /warehouse/grpo/fixed-asset)
  Needs PO   GET needs-po/                      -> Link PO (late link) | Done in SAP by hand
             GET entries/<id>/open-pos/?supplier_code=   open item lines in FIXED_ASSET_WAREHOUSES
             POST entries/<id>/po-receipts/               POReceipt + POItemReceipt rows (accepted = received)
  Ready      GET pending/ -> GET entries/<id>/ + entries/<id>/preview/
             POST post/ (multipart; grpo_posting_id reuses an open DRAFT/FAILED draft, or a stranded QUEUED one; no file -> gate photos)
               -> FixedAssetGRPOService.save_grpo_draft (type, closure, cut-over checked)
               -> sap_postings.post_now(kind="fixed_asset_grpo.post")
               -> FixedAssetGRPOHandler: enabled? -> simulate? -> live guard -> read-back -> post_saved_grpo
               -> OPDN: lines based on the PO (BaseType 22), one WarehouseCode, AttachmentEntry
  History    GET history/, history/<id>/, history/<id>/print/, POST history/<id>/discard-simulated/
```

The SAP document is the engine's (spec §2.6.5). It carries no `Series` and its date is the posting date the screen sends.

```json
{"CardCode":"VENDA001768","BPL_IDAssignedToInvoice":2,
 "Comments":"App: FactoryApp v2 | User: … | PO: 221026008 | Gate Entry: <entry_no>",
 "DocDate":"2026-10-03","DocDueDate":"2026-10-03","TaxDate":"2026-10-03","NumAtCard":"ABD/26-27/814",
 "DocumentLines":[{"ItemCode":"CG0000073","Quantity":"1.000","BaseEntry":14180,"BaseLine":0,"BaseType":22,
   "WarehouseCode":"BH-FA","UnitPrice":1270.0,"TaxCode":"GST18","AccountCode":"1204023",
   "U_Variety":"CANOLA","CostingCode":"CANOLA"}],
 "AttachmentEntry":4567}
```

**Idempotency** comes from the engine's read-back, `find_app_posted_grpo`. Before every real try, the handler asks HANA for an OPDN of the same vendor that carries `Gate Entry: <entry_no>` and has exactly the draft's PO DocEntries. If one exists, it adopts that document instead of posting a second one. If HANA cannot be read, the try **waits**; it never assumes the document is missing.

## What it stores

| Model | Table? | What it is |
|---|---|---|
| `FAReceipt` / `FAItemReceipt` | no (proxies) | Proxies over `raw_material_gatein.POReceipt` / `POItemReceipt`, filtered to FIXED_ASSET entries. They add an admin view (read-only). |
| `FixedAssetGRPOClosure` | new | "Done in SAP by hand". OneToOne to `VehicleEntry` (`related_name="fa_grpo_closure"`). Fields: `sap_doc_num`, `note`, `closed_by`, `closed_at`. Reopening deletes the row. |
| `FixedAssetGRPORecord` | new | One row per POSTED `GRPOPosting` this app made (`related_name="fa_record"`). Fields: `simulated`, `sap_payload` (for a simulated post, the document it would have sent), `posted_by`, `created_at`. It carries the app's three permissions. It replaces the spec's `GRPOPosting.simulated` column. |

The late link **creates, updates and deletes rows** in `POReceipt` / `POItemReceipt`. That is data, not a schema change. The `VehicleEntry` is **never saved**, because a completed entry is locked and `GateEntryBase.save` would raise.

Migrations: `0001_initial` (both proxies and both tables) and `0002_fixed_asset_grpo_group`. Both were written by hand. `0001` depends on `driver_management 0015`, `grpo 0020` and `raw_material_gatein 0015`.

## Endpoints

All of them are mounted at `api/v1/fixed-asset-grpo/`. Every request needs `IsAuthenticated` and `HasCompanyContext` (the `Company-Code` header). Every lookup is scoped to `request.company.company` **and** `entry_type="FIXED_ASSET"`, so anything else is a 404. Route names are `fixed-asset-grpo-<thing>`.

| Method | Path | Right | What |
|---|---|---|---|
| GET | `summary/` | view | grpo's summary numbers, counted over FA only, plus `queued_count`, `draft_count`, `simulated_count`, `needs_po_count` and `closed_count`. |
| GET | `all-entries/` | view | Every FA entry in scope, with a `state`: AT_GATE, NEEDS_PO, READY, SENDING, SIMULATED, POSTED or CLOSED. Query: `year`+`month`, `search`, `state`, `include_closed=1`. Includes `counts`. |
| GET | `pending/` | view | "Ready": completed FA entries whose linked PO is not posted. Same rows as grpo's `pending/`, plus `work_order_number`, `fa_supplier_name` and `invoice_number`. |
| GET | `needs-po/` | view | Completed FA entries with no PO yet: `vehicle_entry_id, entry_no, entry_time, status, work_order_number, supplier_name, invoice_number, item_count`. |
| GET | `history/` | view | FA postings, using grpo's posting serializer plus `simulated` and `simulated_payload`. Query: `vehicle_entry_id`, `year`+`month`, `status`, `search`, `simulated=true\|false`. |
| GET | `history/<posting_id>/` | view | One posting. |
| GET | `history/<posting_id>/print/` | view | grpo's GRN print, scoped first. A simulated post has no SAP document, so it returns 404. |
| POST | `history/<posting_id>/discard-simulated/` | post | Deletes a simulated post, resets the accepted quantity of every line of its PO receipts to the received quantity, and marks its SAP postings row CANCELLED. Returns 204. |
| GET | `entries/<id>/` | view | The entry, its asset lines, the active gate photos, the PO link(s), `link_lock_reason`, `open_draft`, `posted_grpos`, `closure`, and `sap:{enabled, simulate, allow_live, is_test_db, cutover, warehouses}`. |
| GET | `entries/<id>/po-receipts/` | view | The link(s), with `is_editable` / `lock_reason`. |
| POST | `entries/<id>/po-receipts/` | link | Links a PO. Body is raw_material_gatein's `POReceiveRequestSerializer` plus `invoice_no?`. Returns 201 `{message, po_receipt, warnings}`. |
| PUT | `entries/<id>/po-receipts/<po_receipt_id>/` | link | Changes the link (PO, lines, quantities). Returns 200 `{po_receipt, warnings}`. |
| DELETE | `entries/<id>/po-receipts/<po_receipt_id>/` | link | Unlinks the PO. Returns 204. |
| GET | `entries/<id>/open-pos/?supplier_code=` | link | Open PO item lines of the supplier in `FIXED_ASSET_WAREHOUSES`, with warehouse, tax, G/L and variety. Returns `{results, warehouses, over_receipt_enforced}`. |
| GET | `entries/<id>/preview/[?po_receipt_ids=]` | view | grpo's preview (`GRPOPreviewSerializer`). |
| POST / DELETE | `entries/<id>/close/` | post | POST marks the entry done in SAP by hand; the body `{sap_doc_num?, note?}` needs one of the two. Returns 201. DELETE reopens it and returns 204. |
| POST | `post/` | post | Saves and sends the GRPO. See below. |
| DELETE | `drafts/<posting_id>/` | post | Discards an unsent draft: DRAFT, FAILED, or QUEUED whose SAP postings row was cancelled. Returns 204. Returns 400 while the queue holds a live row for it. |

### `POST post/`

The request is multipart: grpo's GRPO request as JSON in a `data` part, and files in `attachments` parts. A JSON body with no files also works. Optional `grpo_posting_id` in `data` reuses that entry's open DRAFT/FAILED draft, or a QUEUED one whose SAP postings row was cancelled (it is turned back to FAILED first). A draft the queue still holds answers 409 `SAP_QUEUED`.

| Answer | When |
|---|---|
| 400 `code: FA_GRPO_DISABLED` | `FIXED_ASSET_GRPO_ENABLED` is off. |
| 400 | The request is invalid, the entry is closed or before the cut-over, the posting date is in the future, `grpo_posting_id` is not an open draft of this entry, or there is no file and no gate photo. |
| 400 `code: FA_GRPO_MIXED_WAREHOUSES` | The posting lines sit in more than one warehouse. |
| 404 | Not an FA entry of this company. |
| 409 `code: SAP_QUEUED` | A GRPO of these POs, or the `grpo_posting_id` draft, is already on its way to SAP. |
| 201 `{success, grpo_posting_id, sap_doc_entry, sap_doc_num, sap_doc_total, message, attachments, simulated}` | Posted, or simulated (`simulated: true`, no SAP numbers). |
| 202 `{queued: true, code: SAP_QUEUED, grpo_posting_id, sap_posting_id}` | SAP is not answering. The draft waits and the worker posts it. |
| 400 / 502 | Refused (`SAPDataError` gives 502). The draft stays FAILED with its data and files. The "Fixed Asset GRPO" group is notified. |

Each request makes at most one SAP document write, so a request takes at most one writer timeout (180 s). The screen should send a 5-minute client timeout, as `grpo.api.ts` does.

## Flags

Each flag is read with `getattr(settings, NAME, default)` in `flags.py`, on every call. `settings.py` is not edited.

| Flag | Default | Effect |
|---|---|---|
| `FIXED_ASSET_GRPO_ENABLED` | `DEBUG` | When off, `post/` answers 400 `FA_GRPO_DISABLED` and the handler leaves queued rows **waiting** (nothing is read or written). |
| `FIXED_ASSET_GRPO_SIMULATE` | `DEBUG` | When on, every check runs and SAP is read but never written. The GRPO is recorded as a simulated post. |
| `FIXED_ASSET_GRPO_ALLOW_LIVE` | `False` | When off, a real send goes only to a company whose Service Layer DB **and** HANA schema start with `TEST_` (`sap_purchasing.live_guard`). |
| `FIXED_ASSET_GRPO_CUTOVER` | `""` | `YYYY-MM-DD`. Entries that came in before it are hidden from every list and refused by post. A value that is not a date raises `ImproperlyConfigured`. |
| `FIXED_ASSET_WAREHOUSES` | `"BH-FA,DL-FA,GP-FA"` | The warehouses whose open PO lines count as fixed-asset lines. |

The handler checks them in a fixed order: **enabled, then simulate, then the live guard**. Simulate comes before the guard so that a dry run works on a server that points at live SAP.

**These are Django settings, not environment variables.** `config/settings.py` does not define them on this branch, so a line in `.env` changes **nothing**. Set them in a settings module that does `from .settings import *`, or add `config(...)` lines for them to `settings.py` when merging.

- With `DEBUG=False` and nothing set (a production server), the feature is **off**: `post/` answers `FA_GRPO_DISABLED`.
- Going live needs `FIXED_ASSET_GRPO_ENABLED=True`, `FIXED_ASSET_GRPO_SIMULATE=False` and `FIXED_ASSET_GRPO_ALLOW_LIVE=True`, plus a `FIXED_ASSET_GRPO_CUTOVER` date.

## Permissions and group

| Codename (`fixed_asset_grpo.*`) | Grants | Implied by |
|---|---|---|
| `can_view_fixed_asset_grpo` | Lists, entry, preview, history, print | either right below |
| `can_link_fixed_asset_po` | Link, change or unlink the PO; read open POs | `can_post_fixed_asset_grpo` |
| `can_post_fixed_asset_grpo` | Post or simulate, close or reopen, discard drafts and simulated posts | — |

- Migration `0002` creates the empty group **"Fixed Asset GRPO"** holding all three rights. Who joins it is OPEN-3.
- None of grpo's rights opens these endpoints. `grpo.add_grpoposting` alone gets 403 here.
- Linking works on **any** FA entry, completed and locked ones included. It is refused only when:
  1. the entry is cancelled;
  2. the entry is closed by hand;
  3. a real GRPO of the PO is posted (the message names its DocNum);
  4. a simulated GRPO exists ("discard it in Fixed Asset GRPO history first");
  5. a GRPO is on its way to SAP.
- **One PO per entry** in v1.
- A change or an unlink first **discards unsent drafts** (DRAFT/FAILED, or QUEUED with a cancelled queue row; no SAP document, no live queue row), because their saved payload names line ids the change may replace.
- **"On its way to SAP"** means: PENDING (inside the engine's post), or DRAFT/FAILED/QUEUED **with a live (`SENDING`/`QUEUED`) row** of kind `fixed_asset_grpo.post` or `grpo.material`. A QUEUED draft whose row was cancelled on the SAP postings screen is *stranded*, not on its way: `sap_postings.services.cancel` leaves the draft QUEUED, so counting it would lock the link, refuse "done in SAP by hand" and refuse a discard or re-send for good. grpo's `_grpo_on_its_way` reads it the same way.
- **Simulated posts and the overlap warning.** A simulated post is stored POSTED, and the gate's `po_line_bookings` skips POSTED GRPOs (it takes their share as gone from SAP's `OpenQty`). Nothing was sent, so SAP still shows that quantity open. When the gate's warning says nothing, `services.po_link.simulated_overlap_warning` adds the simulated quantity on the same PO line and warns if the line is over-promised. Advisory only, as the gate's.

## The queue handler

`sap_posting.FixedAssetGRPOHandler` handles kind `fixed_asset_grpo.post`. `FixedAssetGrpoConfig.ready()` registers it into `sap_postings.services.HANDLERS`, `KIND_LABELS` and `KIND_SHORT` with `setdefault`. It mirrors `grpo.sap_posting.MaterialGRPOHandler`, which is not edited:

1. If the draft is gone, the outcome is *rejected*. If the entry is not FIXED_ASSET, *rejected*. If the draft is already POSTED, *posted* (simulated or real).
2. If the app is switched off, `mark_grpo_waiting_for_sap` runs and the outcome is *waiting*.
3. If simulate is on, `simulate_saved_grpo` runs and the outcome is *posted* with `simulated: true`.
4. The live guard runs. A refusal marks the draft FAILED; the outcome is *rejected* and no SAP call is made.
5. `find_app_posted_grpo` runs. If it finds a document, the handler adopts it.
6. Otherwise `post_saved_grpo` runs.

Errors map the way the material handler maps them:

| Error | Outcome |
|---|---|
| `SAPConnectionError` / `dbapi.Error` | *waiting* (QUEUED) |
| `ValueError`, `SAPValidationError`, `SAPDataError`, or a payload that no longer validates | *rejected* (FAILED) |
| anything else | FAILED, then re-raised |

The person recorded on the post is `draft.posted_by` (whoever last saved or pressed Post), falling back to the queue row's `created_by`. After a real post or an adopt, the handler writes a `FixedAssetGRPORecord` and notifies the "Fixed Asset GRPO" group.

Links point at `FRONTEND_BASE = "/warehouse/grpo/fixed-asset"` (`constants.py`):
- draft: `/preview/<vehicle_entry_id>?draft=<id>`
- posted: `/history/<id>`

If the frontend module mounts elsewhere, change that one constant.

## Simulate: how it works and why

The override preferred a wrapper around the engine's SAP client that passes reads through and fakes writes. **The engine has no hook for one.** `post_grpo` calls the module-level `grpo.services.SAPClient(...)` directly for open quantities, batch flags, the upload and the create, and there is no accessor a subclass could override. Replacing that module global at run time would change grpo for every thread. So the override's fallback was taken, in its strongest form.

`FixedAssetGRPOService.simulate_saved_grpo` is the twin of `post_saved_grpo`. It accepts the same statuses and re-validates the payload the same way. On a refusal it keeps the draft FAILED with its data, just as `post_saved_grpo` does. In between, it:

- re-runs every check `post_grpo` makes, with the same messages: entry, receipts, supplier and branch, gate status, already posted, item ids, batches against SAP's batch-managed flags, and over-receipt against SAP's **live open quantities**. These parts are marked `MIRRORS post_grpo` in the code and must follow any change to the engine.
- builds the exact document `post_grpo` would send, and stores it in `FixedAssetGRPORecord.sap_payload`.
- **writes nothing to SAP.** It makes no `upload_attachment` and no `create_grpo` call. It does not write the gate's weighment either.
- turns the draft row itself into a POSTED posting with no SAP numbers, with its line postings, and keeps its files (status PENDING). It saves with `update()`, so grpo's post_save signal does not tell the "grpo" group anything was posted.

A simulated post blocks a real post of the same PO until it is discarded from history. Discarding:

- resets the accepted quantity of **every** line of the posting's PO receipts to the received quantity, not only the lines that posted (simulate also writes the zero lines it was sent);
- deletes the line postings, the files and the posting;
- marks the SAP postings row that recorded the simulated post (kind `fixed_asset_grpo.post`, POSTED, `result.simulated`) as CANCELLED, with `cancel_reason`, `result.discarded = true` and its link cleared, so the log does not show a "posted" FA GRPO whose page is gone. A direct row update: `sap_postings.services.cancel` takes only waiting or refused rows.

While a simulated post exists:

- it does **not** supersede a real attempt that failed meanwhile (the summary's `failed_count` and history's `is_superseded` leave simulated posts out);
- it counts toward the PO line's overlap warning (see [Permissions and group](#permissions-and-group)).

## Attachments

The engine's behaviour is unchanged.

- **Several files:** `post_grpo` uploads **each file as its own Attachments2 entry** and links only the **last** `AbsoluteEntry` to the GRPO. The earlier entries stay in SAP as orphans. The spec's §2.5.2 fix, which adds further files as lines of one entry, needs an edit to `grpo/services.py` and is deferred. Until then, the screen should prefer a single file.
- **No upload:** when the poster uploads nothing and the draft has no files, the gate entry's **active** `GateAttachment` files (`is_active=True`) are copied onto the GRPO (`services.entries.gate_attachment_files`). With two gate photos, SAP gets two entries and the GRPO links the last. If there is no upload and no gate photo, `post/` answers 400.
- **SAP rule 200019** refuses a GRPO with no attachment unless the vendor's BP group is 101.

## One warehouse per GRPO

The engine sends one header-level `WarehouseCode` for every line. `FixedAssetGRPOService.lines_warehouse` looks at the lines that will post: every line of the chosen receipts whose accepted quantity is above zero, using the request's quantity or else the stored one.

- If those lines sit in **more than one** warehouse, `post/` answers 400 `FA_GRPO_MIXED_WAREHOUSES` ("post one warehouse at a time").
- Otherwise that warehouse is written into the saved payload's `warehouse_code`.

`post_grpo` re-checks this as a backstop.

Because one receipt posts once, a PO with lines in two FA warehouses must be linked one warehouse's lines at a time. The other way is to post with the other warehouse's lines at quantity zero, but those lines can then not be posted from this entry later.

## Residuals (need an edit to an existing app)

1. **grpo's generic draft endpoint can still post an FA receipt.**
   - `/api/v1/grpo/draft/` and `draft/<id>/post/` build a `GRPOService` with no entry-type check in `_load_grpo_draft_targets`.
   - A user holding `grpo.add_grpoposting` can therefore save and post an FA entry's receipt through the raw-material path, if they know its ids. RM `/grpo/post/` and FG `/grpo/fg/post/` behave the same way.
   - That path skips this app's switch, simulate, live guard, cut-over and closure.
   - The fix is the 3-line check of spec §2.5.2 in `grpo/services.py:_load_grpo_draft_targets`. It is deferred until edits to existing files are allowed.
   - This app's handler and lock rules do count live `grpo.material` queue rows as "on its way".
2. **grpo's post_save signal notifies the "grpo" group of a real FA post,** with a raw-material URL (`grpo/signals.py`). The FA group gets its own notification as well.
3. **grpo's catch-all `GRPOPostingDetailAPI` (`/grpo/<id>/`)** has no company scoping and shows FA postings to raw-material viewers. This is pre-existing.
4. **The engine's multi-file Attachments2 behaviour** (see [Attachments](#attachments)).
5. **The engine's `GRPOWriter` does not detect approval drafts** (spec R1). If an approval template catches the B1i user's GRPOs, the document goes to ODRF and the read-back will not find it in OPDN.

## OPEN items

- **OPEN-3:** who joins "Fixed Asset GRPO".
- **R3:** whether CG items are inventory items (`OITM.InvntItem`). The quantity posted is the PO line's quantity.
- **R7:** FA POs keyed to warehouses outside `FIXED_ASSET_WAREHOUSES` do not show up in the picker. Spec S12 shows the open FA PO lines.
- **Cut-over date:** accounts must stop keying JI-gated FA GRPOs by hand from that day (R5).
- **Frontend routes:** confirmed. The frontend module mounts its pages at `FRONTEND_BASE` (`/warehouse/grpo/fixed-asset`). Its menu item sits inside the **Accounts** sidebar group.
- **TEST data:** on the TEST app database (10 Oct 2026) there is one fixed-asset gate entry (GE-2026-4901), and it is still a draft. Every list stays empty until the gate **completes** a fixed-asset entry. Entries appear in *Needs PO* by themselves once completed; SAP is only read when the PO is linked.
- **Phase 2 (not built):** FA GRPO without a gate entry (spec §6.4).

## Tests

```
manage.py test fixed_asset_grpo --settings=config.sqlite_test_settings
```

| File | Covers |
|---|---|
| `tests_flags.py` | Flag defaults, cut-over parsing, and the live guard reading the company's own databases. Uses `SimpleTestCase`. |
| `tests_po_link.py` | The late link on a locked entry, SAP line data, over-receipt / exemption / overlap warning (a simulated post counts toward it), rights (grpo's rights are not enough), one PO, 404 scoping, discarding drafts on update/unlink, and lock reasons (on its way, simulated, real DocNum; a QUEUED draft whose row was cancelled does not lock). |
| `tests_grpo.py` | Switch off (400 and waiting), the post right, closure and cut-over, future date, mixed warehouses, the spec §2.6.5 document, gate-photo fallback, SAP down then 202 then 409, a cancelled wait sent again, a draft the queue still holds not re-used, retry reusing the failed draft, the live guard (no SAP call; ALLOW_LIVE passes), simulate (no writer called, validations still run and read SAP), simulate then discard then real (the refused real attempt is not superseded meanwhile), discard restoring zero lines and cancelling the queue row, notifications (FA group; none when simulated), the handler (registered, posts as `draft.posted_by`, refuses a non-FA draft, adopts what the read-back finds without posting twice, waits when HANA is down), and links. |
| `tests_lists.py` | Lists scoped to FA and company, closure and cut-over, FA-only summary, history `simulated`, 404 for other types and companies (detail, preview, PO link, history, print), rights, and close/reopen rules (a QUEUED draft whose row was cancelled lets the entry close and the draft be discarded). |
| `tests_groups.py` | Migration 0002's functions: three rights, idempotent, grpo's group untouched, reverse. |

SAP is mocked everywhere through these patch targets:
- `grpo.services.SAPClient`
- `grpo.services.CompanyContext`
- `grpo.services.HanaConnection` (using `grpo.tests_sap_queue._hana`)
- `sap_purchasing.fa_po_reader.get_open_fixed_asset_pos`
- `sap_purchasing.live_guard.CompanyContext`

Shared fixtures live in `fa_test_support.py`, which is not collected as a test module.

**Run on 10 Oct 2026:** 83 tests, all pass, with `config.sqlite_test_settings` and every outside host pointed at a dead local port. `makemigrations fixed_asset_grpo --check --dry-run` reports no changes. The screens were opened against the TEST app database in SIMULATE mode. Nothing has been posted to SAP, not even to TEST SAP: TEST has no completed fixed-asset gate entry to post.

## Registration (lead only)

- `config/settings.py` `INSTALLED_APPS`: add `'fixed_asset_grpo.apps.FixedAssetGrpoConfig'`, after `grpo`, `raw_material_gatein`, `fixed_asset_gatein` and `sap_postings`.
- `config/urls.py`: add `path("api/v1/fixed-asset-grpo/", include("fixed_asset_grpo.urls"))`.
- Then run `manage.py makemigrations fixed_asset_grpo --check --dry-run --settings=config.sqlite_makemigrations_settings` to confirm the hand-written migrations match the models.
