# SAP Documents

> Django app: `sap_documents` · Base URL: `/api/v1/sap-documents/`
> Frontend: `/sap-documents` (`FactoryFlow/src/modules/sap-documents`)
> Came from: SAP Portal (`backend_v1`) — `public/documents.html` and the
> `/api/sap/documents/*`, `/payment-drafts/:entry` and `/attachments/*` routes
> in `routes/sap.js` (~1399–2666).

SAP Portal's document browser, rebuilt inside JI. Pick a document type, narrow
the list by number, partner, date and status, and open a document to see:

* its header — partner with **their** GSTIN, PAN and state, our branch and
  branch GSTIN, sales employee, payment terms and shipping type by name, the
  references, dates and remarks;
* its lines — item, quantity, price, warehouse, G/L account, SAC code, location
  and cost dimensions by name, plus the freight/service UDFs (received and
  dispatched quantity, litres, bilty no., …);
* its totals — net, tax, withholding, rounding, total, paid to date and the
  balance still due;
* the withholding tax (TDS) by section, the documents its lines were copied
  from, and the journal entry SAP posted for it (for an A/P or A/R invoice also
  the journal of the GRPO or delivery behind it);
* for a draft waiting for approval, a **reconstructed journal** — SAP has not
  posted one yet — and for a draft that has been added, the document it became
  and that document's journal;
* its attachments and the attachments of the documents it was copied from
  (the scan is often filed there), each downloadable with the download right.

Fourteen document types, exactly SAP Portal's whitelist: purchase orders, GRPOs,
A/P invoices and credit notes, goods returns, A/R invoices and credit memos,
return notes, inventory transfers and transfer requests, journal entries,
outgoing payments, drafts and outgoing-payment drafts.

What it is **not**: `sap_finance` (the ledgers, chart of accounts and budgets),
`universal_search` (finds one number across every company and module) or the
approval queues (which decide drafts). Nothing here writes to SAP.

## Where the data comes from

| Read | Channel | Why |
|--|--|--|
| Lists and documents | Service Layer (`ServiceLayerEntityClient`) | The portal's `$select` lists were corrected against live SAP; a document's Service Layer shape already carries its lines, UDFs and India localisation. |
| Names, settlement, base documents, journals, draft preview | HANA (`sap_client/hana/document_reader.py`) | One connection per screen; every lookup one `IN` statement over all lines, so the number of statements does not grow with the lines. Decoration: when HANA cannot be read the document still opens, with a warning. |
| Outgoing-payment drafts | HANA (OPDF, PDF1/2/4/6) | The Service Layer does not expose them as payments. |
| Attachment list | HANA (`ATC1`) | `AbsEntry` + `Line` is the key the file service serves by. |
| Attachment files | SAP file service (`SAPClient.download_attachment`) | By entry and line first — names like `1825.pdf` collide across documents — then by name. |

Everything goes through `SAPClient` for the `Company-Code` company.

## Endpoints

| Method | Path | Right |
|--|--|--|
| GET | `types/` — the fourteen types, the filters each takes | `can_view_sap_documents` |
| GET | `documents/<type>/` `?number ?partner ?date_from ?date_to ?status=O\|C\|L ?top (≤100) ?skip` | `can_view_sap_documents` |
| GET | `documents/<type>/<doc_entry>/` (JdtNum for journal entries) | `can_view_sap_documents` |
| GET | `payment-drafts/<doc_entry>/` (same as `documents/PaymentDrafts/<doc_entry>/`) | `can_view_sap_documents` |
| GET | `attachments/<abs_entry>/` — the files of one attachment entry | `can_view_sap_documents` |
| GET | `attachments/<abs_entry>/<line>/download/` — the file | `can_view_sap_documents` **and** `can_download_sap_attachments` |

An unknown type is 400 before SAP is asked. SAP's refusal → 400 with SAP's
words, SAP unreachable (or the file service not configured) → 503, SAP broken →
502, no such document / attachment line / file → 404. `status` is offered only
where the type has one (marketing documents O/C/L, drafts and transfer requests
O/C).

A download is recorded in `SapAttachmentDownload` (company, entry, line, file
name, size, who, when) after the file service returned it. PDFs, pictures and
plain text are sent `inline`; everything else as an `attachment` of
`application/octet-stream`. The name goes in an ASCII `filename=` plus an RFC
5987 `filename*=UTF-8''…` when it is not ASCII (scanner apps put U+202F, en
dashes and ₹ in names).

## Differences from SAP Portal

* **Rights are enforced by the server, per company.** The portal let any login
  read any company's documents (`?company=`) and download any attachment. Here
  browsing needs `can_view_sap_documents`, downloading also
  `can_download_sap_attachments`, and the company is the `Company-Code` header's.
* **Linked journals are found by key.** The portal searched `OJDT` for any
  entry whose memo or references *contained* the document's number
  (`LIKE '%123%'`) and took the newest, so it could show an unrelated journal.
  Here: the document header's `TransId`; `OJDT.TransType` + `CreatedBy` for
  transfers and payments; the base GRPO/delivery's `TransId` for the in-transit
  entry; `draftKey` for a draft that has been added.
* **The draft journal preview covers invoices and credit notes only** (object
  types 13, 14, 18, 19) — the documents the portal's rules were verified on. The
  portal also ran them for GRPO and PO drafts, where they credited the vendor's
  control account; SAP posts a GRPO against goods-received-not-invoiced and a PO
  not at all.
* **Errors are errors.** The portal answered a list SAP refused with an empty
  list (`success: true`). Here it is 400/502/503 with the reason.
* **"Cancelled" works.** The portal filtered on `DocumentStatus eq 'bost_Cancel'`,
  which is not a status, so SAP refused and the list came back empty. Here it is
  `Cancelled eq 'tYES'`, and "Closed" excludes cancelled documents.
* **The partner search matches code or name**, in the case typed and in
  capitals; the portal matched the name only, as typed.
* **Net amount adds the withholding back** (`DocTotal` is net of TDS; the portal
  showed net short by the TDS on such documents).
* **A draft shows no balance due**, and a transfer ships from its *from*
  warehouse (the portal used the lines' target warehouse).
* **Payment-draft approval states are SAP's.** The portal read `WddStatus`
  `N` as "Open" and `Y` as "Closed"; they are Rejected and Approved.
* **HTML/SVG attachments never open inline**, and the share path of a file is
  not sent to the browser.
* **Not ported:** reading attachments straight off the Windows share with
  `smbclient` (the file service is the only channel — see
  `sap_client/docs/sap_portal_port.md`), and the portal's fuzzy journal search.

## Setting it up on a live database

1. Set `SAP_FILE_SERVICE_BASE_URL` in the server's `.env` (the portal's
   `FILE_SERVICE_BASE`), or downloads answer 503 "not configured". Browsing
   works without it.
2. `manage.py migrate sap_documents` creates the download log and the two
   permission rows.
3. `manage.py setup_sap_documents_groups` creates *SAP Documents - Viewer* and
   *SAP Documents - Viewer with attachments*. It puts nobody in them.
4. Add the users who need it to a group (each needs an active `UserCompany` for
   the company they browse). Until then the module is invisible.

## Tests

`sap_documents/tests.py` — the permission stack on every endpoint (no header,
another company, each right, download needs both), the type whitelist, the
filters as they reach the Service Layer, the detail shaping (backfilled and
named lines, honest GSTINs, ship-from by address code, totals, drafts, journal
entries, transfers, payments), the download (streaming, audit row, headers,
404/503/502) and the group command.
`sap_client/tests_document_reader.py` — the HANA reader against a fake cursor
(batched statements, bound values, soft failures, journals by key, draft
preview and added drafts, ATC1, payment drafts) and the journal-preview builders
with the portal's verified cases. SAP is mocked throughout.
