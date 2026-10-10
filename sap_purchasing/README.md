# sap_purchasing

SAP purchasing plumbing for **Cash Book → SAP** (`cash_sap`) and **Fixed Asset GRPO** (`fixed_asset_grpo`), built on branch `feat/fixed-asset-grpo`.

This package replaces the spec's unit U1 (`FA_GRPO_BUILD_SPEC.md` §2.1). The spec put this code inside `sap_client`. `FA_GRPO_SEPARATE_MODE.md` §2.1 moves it here so that `sap_client` stays untouched.

- It is a **plain Python package**: no models, no migrations, and it is not in `INSTALLED_APPS`.
- It **imports** `sap_client` (context, exceptions, `_ServiceLayerDocWriter`, `TransferDocumentWriter._error_message`, `errors.unanswered`, `HanaConnection`, `HanaSeriesReader`, `HanaPOReader`, `OPEN_LINE_COLUMNS`, `PODTO`) and never edits it.
- **Every writer and reader takes a `company_code`** (`"JIVO_OIL"`, `"JIVO_BEVERAGES"`, `"JIVO_MART"`) and builds `sap_client.context.CompanyContext(code)` itself. A handler running for Oil can therefore write into Beverages by naming it.
- **Nothing here reads a simulate flag or an enable flag.** Callers check them in this order: enabled → simulate (no SAP call at all) → `live_guard.require_test_target` → the calls below.

## Files

| File | What it holds |
|---|---|
| `__init__.py` | Package marker. It imports nothing, because `sap_client.registry` reads settings at import time. |
| `live_guard.py` | The TEST_-only guard (spec §2.1.4, D8). |
| `service_layer.py` | Writers for `PurchaseOrders`, `PurchaseDeliveryNotes` and `Drafts` (A/P invoice drafts), plus PO Close and `DraftsService_SaveDraftToDocument`. Detects approval drafts. |
| `readback.py` | Marker read-back on OPOR / OPDN / OPCH / ODRF (schema-qualified), `document_lines` / `document_state` / `num_at_card_taken` / `item_flags`, and the pure line mappers: FreeText for PO lines, BaseLine for GRPO lines. |
| `series.py` | The series for (object, date, branch[, hint]) (spec §2.1.3). |
| `fa_po_reader.py` | Open PO item lines for a supplier in `FIXED_ASSET_WAREHOUSES` (spec §2.1.5), returned as the same `PODTO` / `POItemDTO` as `sap_client/hana/po_reader.py`. |
| `client.py` | `PurchasingClient(company_code)`: one object with the spec §2.1.6 method names. **Code against this.** |
| `tests_*.py` | `SimpleTestCase` unit tests with mocked HTTP and cursors. |

## Import cheat-sheet

```python
from sap_purchasing.client import PurchasingClient
from sap_purchasing import live_guard
from sap_purchasing.service_layer import is_pending_approval, OBJ_PURCHASE_ORDER, OBJ_GRPO, OBJ_AP_INVOICE
from sap_purchasing.readback import (
    po_line_free_text, map_lines_by_free_text, map_lines_by_base_line, classify_draft,
    DRAFT_IN_APPROVAL, DRAFT_APPROVED, DRAFT_REJECTED, DRAFT_OTHER,
)
from sap_purchasing.fa_po_reader import get_open_fixed_asset_pos, fixed_asset_warehouses
from sap_client.exceptions import (
    SAPConnectionError, SAPUnavailable, SAPOutcomeUnknown, SAPValidationError, SAPDataError,
)
```

---

## `client.PurchasingClient(company_code: str)`

This class replaces the spec's `SAPClient` additions. The names and arguments are the spec's, so the spec's handler pseudo-code works with `client = PurchasingClient(code)`. Each call builds its writer or reader on demand, and nothing is cached.

| Method | Returns | Delegates to |
|---|---|---|
| `create_purchase_order_document(payload: dict)` | `dict`: SAP's created JSON, or the **approval-draft shape** | `service_layer.create_purchase_order` |
| `close_purchase_order(doc_entry: int)` | `None` | `service_layer.close_purchase_order` |
| `create_purchase_delivery_note_document(payload: dict)` | `dict`: created JSON, or the approval-draft shape | `service_layer.create_purchase_delivery_note` |
| `create_purchase_invoice_draft(payload: dict)` | `dict`; **`["DocEntry"]` is the ODRF draft entry** | `service_layer.create_purchase_invoice_draft` |
| `save_purchasing_draft_to_document(object_code: str, draft_entry: int)` | `None` (SAP returns no body) | `service_layer.save_draft_to_document`; only `"22"` / `"20"` |
| `find_document_by_marker(object_code, card_code, marker)` | `dict \| None` | `PurchasingReadback.find_by_marker` |
| `find_document_by_draft_key(object_code, draft_entry)` | `dict \| None` | `PurchasingReadback.find_by_draft_key` *(extra)* |
| `find_draft_by_marker(object_code, card_code, marker)` | `dict \| None` | `PurchasingReadback.find_draft_by_marker` |
| `document_lines(object_code, doc_entry)` | `list[dict]` | `PurchasingReadback.document_lines` |
| `document_state(object_code, doc_entry)` | `dict \| None` | `PurchasingReadback.document_state` |
| `num_at_card_taken(object_code, card_code, num_at_card)` | `bool` | `PurchasingReadback.num_at_card_taken` |
| `item_flags(item_codes: Iterable[str])` | `dict[str, dict]` | `PurchasingReadback.item_flags` |
| `resolve_series_for_branch(object_code, posting_date, bpl_id=None, name_hint="")` | `dict` (send `["series"]`) | `series.resolve_series_for_branch` |
| `get_open_fixed_asset_pos(supplier_code, warehouses=None)` | `list[PODTO]` | `fa_po_reader.get_open_fixed_asset_pos` |
| `is_test_target()` | `bool` | `live_guard.is_test_target` |
| `upload_attachment(file_path, filename, *, allow_metadata_fallback=False)` | `dict` with `"AbsoluteEntry"` | `SAPClient(code).upload_attachment` (pass-through) |
| `add_line_to_existing_attachment(absolute_entry, file_path, filename, *, allow_metadata_fallback=False)` | `dict` | `SAPClient(code).add_line_to_existing_attachment` (pass-through) |

**Object codes** are strings: `"22"` is PO (OPOR/POR1), `"20"` is GRPO (OPDN/PDN1), `"18"` is A/P invoice (OPCH/PCH1). Any other code raises `ValueError` before SQL or HTTP.

**Patching in tests:** patch `<your_module>.PurchasingClient` when you imported the class. Patch `sap_purchasing.live_guard.CompanyContext` for the guard.

---

## `live_guard`

```python
target_databases(company_code: str) -> tuple[str, str]   # (service_layer["company_db"], hana["schema"])
is_test_target(company_code: str) -> bool                # both start with "TEST_" (case-insensitive)
require_test_target(company_code: str, *, allow_live: bool, switch_name: str) -> None
```

- `require_test_target` returns `None` when `allow_live` is true or the company is a test target. Otherwise it raises **`ValueError`**: `"<code> is live SAP (<dbs>) and <switch_name> is off: only TEST_ company databases can be posted to."`
- The check reads `CompanyContext(code)` on every call, so it tests the databases the writers **actually** use, not `settings.COMPANY_DB`.
- A blank database name counts as live (fail closed).
- An unknown company code raises `SAPValidationError` from the registry, in both functions. Treat it as a rejection.
- Callers pass their own switch, for example `require_test_target(code, allow_live=flags.allow_live(), switch_name="CASH_SAP_ALLOW_LIVE")` or `switch_name="FIXED_ASSET_GRPO_ALLOW_LIVE"`.

---

## `service_layer`

### Functions

```python
create_purchase_order(company_code, payload) -> dict            # POST /b1s/v2/PurchaseOrders        timeout 120
create_purchase_delivery_note(company_code, payload) -> dict    # POST /b1s/v2/PurchaseDeliveryNotes timeout 180
create_purchase_invoice_draft(company_code, payload) -> dict    # POST /b1s/v2/Drafts               timeout 120
close_purchase_order(company_code, doc_entry) -> None           # POST /b1s/v2/PurchaseOrders(n)/Close  timeout 60
save_draft_to_document(company_code, object_code, draft_entry) -> None
    # POST /b1s/v2/DraftsService_SaveDraftToDocument {"Document":{"DocEntry":n,"DocObjectCode":...}}
    # "22" -> oPurchaseOrders (timeout 120), "20" -> oPurchaseDeliveryNotes (timeout 180); else ValueError
is_pending_approval(result) -> bool
json_native(obj)        # Decimal->float, date/datetime->"YYYY-MM-DD", tuple->list (recursive)
OBJ_PURCHASE_ORDER = "22"; OBJ_GRPO = "20"; OBJ_AP_INVOICE = "18"
```

### Classes

These are subclasses of `sap_client.service_layer.delivery_note_writer._ServiceLayerDocWriter`. Construct them with a `CompanyContext`.

| Class | Settings and behaviour |
|---|---|
| `PurchaseOrderDocWriter` | `endpoint="PurchaseOrders"`, `label="Purchase Order"`, `DOC_OBJECT_CODE="oPurchaseOrders"`, `timeout=120`, `close_timeout=60`. Adds `.close(doc_entry)`. |
| `PurchaseDeliveryNoteDocWriter` | `endpoint="PurchaseDeliveryNotes"`, `label="GRPO"`, `DOC_OBJECT_CODE="oPurchaseDeliveryNotes"`, `timeout=180`. |
| `PurchaseInvoiceDraftWriter` | `endpoint="Drafts"`, `label="A/P invoice draft"`, `DOC_OBJECT_CODE="oPurchaseInvoices"`, `timeout=120`, **no approval detection**. |

`PurchaseInvoiceDraftWriter` has two more rules:
- `create` copies the payload and does `setdefault("DocObjectCode", "oPurchaseInvoices")`. Any other value raises `SAPValidationError`, and nothing is sent.
- `save_draft_to_document` raises `SAPDataError`, because head office adds A/P drafts in JSAP.

### What the writers send

- **The payload goes out as given.** Builders (`cash_sap/payloads.py`) own the exact JSON of spec §2.6. This module only runs `json_native` on it.
- **The Service Layer session** comes from the base `_get_session_cookies`, which logs in on every call. A failed login raises `SAPUnavailable` and sends nothing.

### Approval-draft shape (PO and GRPO only)

If SAP holds the document in an approval procedure, it answers with a `Location: …/Drafts(N)` header. The writer checks that header before the status code and returns:

```python
{"DocEntry": None, "DocNum": "", "pending_approval": True, "draft_entry": N}
```

Test for it with `is_pending_approval(res)`. Then store `draft_entry`, report IN_APPROVAL, and settle it later through `find_draft_by_marker` (see the recipe below).

### Error mapping (every create, close and save-draft call)

| SAP answer | Result |
|---|---|
| 201 (create) / 200 or 204 (close, save-draft) | success |
| 201 whose body is not JSON | **`SAPOutcomeUnknown`** (the document exists; read it back) |
| 400 | `SAPValidationError(sap message + " (SAP code)")` |
| 401, 403, 502, 503 | `SAPUnavailable` (nothing was posted) |
| 504 | **`SAPOutcomeUnknown`** |
| any other status | `SAPDataError` |
| connect timeout, refused connection, TLS failure | `SAPUnavailable("Unable to connect to SAP Service Layer.")` |
| read timeout, connection dropped mid-answer | `SAPOutcomeUnknown` ("may still have been done") |
| any other `requests.RequestException` (e.g. `ChunkedEncodingError`, answer cut off) | `SAPOutcomeUnknown` |

**What to do with each error in a handler:**
- `SAPConnectionError` (the parent of `SAPUnavailable` and `SAPOutcomeUnknown`) or `dbapi.Error`: return **waiting**, and read back by marker on the next try.
- `SAPValidationError`, `SAPDataError` or `ValueError`: return **rejected**.

---

## `readback`

### `PurchasingReadback(company_code)`

Every method opens one HANA connection and closes it again. Every table is written as `"{schema}"."<TABLE>"`, and that includes ODRF and OITM.

**On failure it never fails open:**
- A connect failure or any `dbapi.Error` raises **`SAPUnavailable`**.
- Two hits on one marker raise **`SAPDataError`**, which names both DocNums or draft entries.
- A blank marker raises `ValueError`.

| Method | Returns |
|---|---|
| `find_by_marker(object_code, card_code, marker)` | `{"doc_entry": int, "doc_num": int, "doc_total": float, "doc_date": "YYYY-MM-DD", "draft_key": int\|None}` or `None`. Matches `CANCELED='N' AND CardCode=? AND Comments LIKE '%<marker>%'`. |
| `find_by_draft_key(object_code, draft_entry)` | Same shape, matched on `draftKey=? AND CANCELED='N'`. Use it after `save_purchasing_draft_to_document`, as an alternative to the marker. |
| `find_draft_by_marker(object_code, card_code, marker)` | `{"draft_entry": int, "wdd_status": str, "doc_total": float, "outcome": DRAFT_*}` or `None`. Searches ODRF with `ObjType=? AND CardCode=? AND DocStatus='O' AND Comments LIKE ?`. `wdd_status` is `'-'` when null. |
| `document_lines(object_code, doc_entry)` | A list ordered by `LineNum`, each `{"line_num", "item_code", "quantity", "open_qty", "price", "line_total", "free_text", "base_entry", "base_line", "line_status"}`. Returns `[]` when the document is missing. |
| `document_state(object_code, doc_entry)` | `{"doc_num": int, "canceled": "Y"\|"N"\|"C", "doc_status": "O"\|"C"}` or `None`. |
| `num_at_card_taken(object_code, card_code, num_at_card)` | `True` when the header table has `CANCELED='N'` with the same CardCode and NumAtCard, **or** an open ODRF draft of that ObjType has them. The comparison is exact, so `.` suffixes count as different. |
| `item_flags(item_codes)` | `{code: {"item_name", "group": int, "inventory": bool, "purchase": bool, "valid": bool, "frozen": bool}}`. Unknown codes are absent, and an empty input runs no SQL. |

### Pure helpers (no SAP)

```python
OBJECT_TABLES = {"22": ("OPOR","POR1"), "20": ("OPDN","PDN1"), "18": ("OPCH","PCH1")}
PO_LINE_FREE_TEXT = "JI V{entry_id}"
po_line_free_text(entry_id) -> str                       # "JI V12345" -> the PO line's "FreeText"
map_lines_by_free_text(lines, free_texts: {key: text}) -> {key: LineNum}
map_lines_by_base_line(lines, po_lines: {key: po_line_num}, base_entry=None) -> {key: GRPO LineNum}
classify_draft(wdd_status) -> "IN_APPROVAL" ('W') | "APPROVED" ('Y') | "REJECTED" ('N') | "OTHER" (anything else)
```

The mappers **leave out** any key whose text or BaseLine appears on no line, or on more than one line. The caller decides what to do about it, for example "PO line not found". The mappers never guess.

### Recipes

**PO line map**, after a create or an adopt:

```python
lines = client.document_lines("22", po_doc_entry)
line_of = map_lines_by_free_text(lines, {v.entry_id: po_line_free_text(v.entry_id) for v in vouchers})
```

The payload builder must set `FreeText = po_line_free_text(entry_id)` on each PO line.

**GRPO line map** (`ImprestGRPO.line_map`, `{voucher_id: LineNum}`):

```python
lines = client.document_lines("20", grpo_doc_entry)
line_map = map_lines_by_base_line(lines, {v.id: v.po_line_num for v in members}, base_entry=po_doc_entry)
```

**Settling a draft** (spec D10):

```python
d = client.find_draft_by_marker(obj, vendor, marker)
if d:
    if d["outcome"] == DRAFT_IN_APPROVAL:
        ...  # waiting: "Waiting for approval in SAP (draft n)."
    elif d["outcome"] == DRAFT_APPROVED:
        client.save_purchasing_draft_to_document(obj, d["draft_entry"])
        doc = client.find_document_by_marker(obj, vendor, marker)  # or find_document_by_draft_key
        ...  # adopt doc, or wait if it is not visible yet
    elif d["outcome"] == DRAFT_REJECTED:
        ...  # rejected: "Rejected in SAP approval"
    else:
        ...  # waiting: SAP holds a plain draft; add or delete it in SAP
```

For an A/P draft (`"18"`), an open ODRF row carrying the marker **is** the success. Adopt `draft_entry` and post nothing.

---

## `series`

```python
resolve_series_for_branch(company_code, object_code, posting_date, bpl_id=None, name_hint="") -> dict
class BranchSeriesReader(HanaSeriesReader):          # build with a CompanyContext
    candidates(object_code, posting_date) -> list[dict]
    resolve_for_branch(object_code, posting_date, bpl_id=None, name_hint="") -> dict
pick_series(rows, object_code, posting_date, bpl_id=None, name_hint="") -> dict   # pure rules
```

- `posting_date` may be a `date`, a `datetime` or an ISO string.
- **Row shape:** `{"series": int, "series_name", "begin_str", "bpl_id": int|None, "next_number", "last_number", "locked": "Y"|"N", "indicator", "period_status"}`.
- **`candidates`** runs the OFPR⋈NNM1 query of `HanaSeriesReader._fetch`, adds `S."BPLId"`, `S."BeginStr"` and `IFNULL(S."Locked",'N')` as columns, and keeps only the rows of the narrowest period that covers the date. Locked rows are **not** filtered in SQL: the narrowest period is chosen over all rows first (the base reader's order), so a month whose series are all locked cannot hand the date to a year-period series.

**The rules**, applied in this order:
1. If `bpl_id` is set, keep that branch's rows when any exist; otherwise keep rows with a null `bpl_id`.
2. If `name_hint` is set, keep rows where `hint.upper()` is in `(series_name + " " + begin_str).upper()`.
3. Drop locked rows (`locked == "Y"`; a row without the key counts as unlocked). This comes after the branch rule, so a branch whose own series are all locked gets "No open SAP series" instead of falling back to a branchless series.
4. Drop exhausted rows (`next_number > last_number`).

**The result:**
- Exactly one row left: return it.
- No rows: `SAPDataError("No open SAP series for object {o} on {dd Mon YYYY} (branch {b}). Ask the SAP team to open it.")`. When locked rows were dropped, `" {names} is/are locked."` is added before "Ask the SAP team".
- More than one row: `SAPDataError("Several SAP series fit object {o} on {date}: {names}. Set the series hint in Cash book → SAP settings.")`.

**Errors:**
- HANA unreachable: `SAPConnectionError`, so the handler waits.
- Query refused: `SAPDataError`.

The FA vendor GRPO does **not** use this. It keeps the engine's behaviour of sending no `Series`.

---

## `fa_po_reader`

```python
DEFAULT_FIXED_ASSET_WAREHOUSES = "BH-FA,DL-FA,GP-FA"
fixed_asset_warehouses() -> list[str]          # getattr(settings, "FIXED_ASSET_WAREHOUSES", default); str "a,b" or list
get_open_fixed_asset_pos(company_code, supplier_code, warehouses=None) -> list[PODTO]
class HanaFixedAssetPOReader(HanaPOReader):    # build with a CompanyContext
    get_open_fixed_asset_pos(supplier_code, warehouses=None) -> list[PODTO]
filter_copy_rows(rows, warehouses) -> list     # pure
```

- **DTOs.** It returns the same `sap_client.dtos.PODTO` / `POItemDTO` as `HanaPOReader.get_open_finished_goods_pos`. Each item has `po_item_code`, `item_name`, `ordered_qty`, `received_qty`, `remaining_qty`, `uom`, `rate`, `line_num`, `tax_code`, `warehouse_code`, `account_code` and `variety`. Each PO has `po_number` (str), `supplier_code`, `supplier_name`, `doc_entry`, `branch_id`, `vendor_ref` and `doc_date`.
- **Live query.** `OPEN_LINE_COLUMNS` plus `OpenQty > 0 AND T0."DocType"='I' AND T1."ItemCode" IS NOT NULL AND T1."WhsCode" IN (?, …)`.
- **Warehouses.** An empty warehouse list (after cleaning) raises `ValueError`.
- **When HANA is unreachable,** the reader answers from the `sap_mirror` copy, filtered here by warehouse and non-blank item code. If there is no copy, the original `SAPConnectionError` / `SAPDataError` is raised.

---

## Tests

Run them with:

```
manage.py test sap_purchasing.tests_live_guard sap_purchasing.tests_service_layer sap_purchasing.tests_readback sap_purchasing.tests_series sap_purchasing.tests_fa_po_reader sap_purchasing.tests_client --settings=config.sqlite_test_settings
```

- They are all `SimpleTestCase`, so they touch no database.
- HTTP, the SL login, HANA connections, `CompanyContext` and the `sap_mirror` copy are all mocked.
- They need `hdbcli` and `requests` importable, as the rest of `sap_client` does.
- If the test runner refuses a non-app label, move them to `cash_sap/tests_sap_purchasing*.py` unchanged. Their imports are absolute.
- **Run on 10 Oct 2026** with the dotted labels above: 87 tests, all pass.

## Deviations from the spec (and why)

1. **Placement.** The code lives in `sap_purchasing/*` instead of `sap_client/{service_layer/purchasing_writers.py, hana/purchasing_readback.py, live_guard.py, client.py, hana/series_reader.py, hana/po_reader.py}`, as the override requires. The `SAPClient` methods became `PurchasingClient`.
2. **Readers take a company code.** `PurchasingReadback(company_code)` replaces the spec's `HanaPurchasingReadback(context)`, and `HanaPurchasingReadback.find_by_marker` is exposed as `PurchasingClient.find_document_by_marker`.
3. **Branch series without editing `series_reader.py`.** `HanaSeriesReader` has no branch filter, so `BranchSeriesReader` subclasses it and adds `candidates`.
4. **FA open POs without editing `po_reader.py` or `sap_mirror/purchase_orders.py`.** The copy fallback filters warehouse and item in Python, because `open_po_rows` has no `warehouses` argument. The copy holds no `DocType`, so a blank item code is the service-line test.
5. **`save_draft_to_document` is overridden, not inherited.** It uses the 502/503/504 split and the writer's own timeout (120 s / 180 s) instead of the base's 60 s and "everything else is a data error". The add runs the full notification procedure, so a 504 there may have committed.
6. **`close()`** maps 401/403/502/503 to `SAPUnavailable` and 504 to `SAPOutcomeUnknown`. `TransferDocumentWriter._action` maps them to `SAPDataError`.
7. **Two extras:**
   - A 201 with an unreadable body raises `SAPOutcomeUnknown` instead of the base's `SAPDataError`.
   - `find_by_draft_key` was added as a read-back path after `SaveDraftToDocument`.
8. **`find_draft_by_marker`** also returns `"outcome"` (`classify_draft`). `wdd_status` is still returned unchanged, as the spec specifies.

## Checked against SAP (9 Oct 2026, read-only)

Run with SELECTs only, inside a READ ONLY transaction that was always rolled back, through the app's own readers against `TEST_JIVO_OIL_HANADB` and with plain queries against the live companies:

- **Every column this package reads exists** in JIVO_OIL, JIVO_BEVERAGES and the `TEST_` copies: `NNM1."BPLId"`/`"BeginStr"`/`"Locked"`, `POR1."FreeTxt"`, `PDN1."BaseType"`/`"BaseEntry"`/`"BaseLine"`, `O*."draftKey"`, `ODRF."WddStatus"`/`"ObjType"`, `OITM."validFor"`/`"frozenFor"`/`"InvntItem"`/`"PrchseItem"`.
- **The readers ran for real** on TEST: `find_by_marker` and `find_draft_by_marker` (OPOR/OPDN/OPCH/ODRF), `document_lines`, `document_state`, `num_at_card_taken`, `item_flags`, `resolve_series_for_branch` (PO `PO1026` 2467 and GRPO `GRPO1026` 2479 resolve alone; A/P is ambiguous on branch 2 -- HR_B, HR_G, CNHR, HR_D -- and needs the hint `HR_B`), `get_open_fixed_asset_pos` (live HANA, not the copy), and the live guard (TEST_ databases pass).
- **Lengths:** Comments 254, NumAtCard 200, line FreeTxt 100 -- the markers fit.

Still not verified (they need a real write on TEST SAP):

- These Service Layer behaviours are taken on the spec's word (§6.2): `PurchaseOrders(n)/Close` on v2; the SL property `FreeText` landing in `POR1.FreeTxt`; created lines numbered in payload order; and a Drafts POST answering 201 with the draft's `DocEntry`.
- A posted document added from a draft is assumed to keep the draft's `Comments`, and so its marker. `find_by_draft_key` is the fallback if it does not.
