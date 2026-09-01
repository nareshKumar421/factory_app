"""SAP boundary for the marketplace confirm flow.

Wraps :class:`sap_client.SAPClient`. When ``settings.MARKETPLACE_SIMULATE_SAP`` is
true (defaults to DEBUG), all SAP calls are skipped and synthetic document numbers
are returned so the scan→confirm flow can be demoed/tested without a live SAP.
"""
import logging
from decimal import Decimal

from django.conf import settings

from .errors import MarketplaceError

logger = logging.getLogger(__name__)


def oitw_onhand(company_code, item_codes, warehouse_code):
    """``{item_code: Decimal on-hand}`` from SAP OITW for a warehouse.

    Best-effort: returns ``{}`` when HANA is unavailable (callers then treat stock
    as unknown and do not block). Shared by the immediate-confirm stock check and
    the bulk delivery-note partition so both use one on-hand source.
    """
    codes = [c for c in {(c or "").strip() for c in item_codes} if c]
    if not codes or not warehouse_code:
        return {}
    try:
        from hdbcli import dbapi
        from sap_client.context import CompanyContext
        h = CompanyContext(company_code).hana
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("On-hand lookup unavailable (%s)", e)
        return {}
    ph = ",".join(["?"] * len(codes))
    sql = (
        f'SELECT "ItemCode","OnHand" FROM "{h["schema"]}"."OITW" '
        f'WHERE "WhsCode"=? AND "ItemCode" IN ({ph})'
    )
    out, conn = {}, None
    try:
        conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                             password=h["password"], encrypt=True, sslValidateCertificate=False)
        cur = conn.cursor()
        cur.execute(sql, [warehouse_code, *codes])
        for item, onhand in cur.fetchall():
            out[item] = Decimal(str(onhand))
        cur.close()
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("On-hand query failed (%s)", e)
        return {}
    finally:
        if conn is not None:
            conn.close()
    return out


def oitm_names(company_code, item_codes):
    """``{item_code: ItemName}`` from the SAP item master (OITM).

    Same HANA pattern and same best-effort promise as :func:`oitw_onhand` — a
    failure is logged and never raised — but the return differs deliberately:

        ``None``  the master could not be read; nothing can be concluded
        ``{}``    the master was read and matched none of the codes
        ``{...}`` the codes it matched

    ``oitw_onhand`` can collapse those two into ``{}`` because "no stock data"
    and "no stock" both simply mean "do not block". Here they are opposites: one
    must let every save through, the other must reject every code. Returning
    ``{}`` for both let unknown codes save cleanly during an outage — and, worse,
    whenever a payload happened to contain only unknown codes.
    """
    codes = [c for c in {(c or "").strip() for c in item_codes} if c]
    if not codes:
        return {}
    try:
        from hdbcli import dbapi
        from sap_client.context import CompanyContext
        h = CompanyContext(company_code).hana
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("Item-master lookup unavailable (%s)", e)
        return None
    ph = ",".join(["?"] * len(codes))
    sql = (
        f'SELECT "ItemCode","ItemName" FROM "{h["schema"]}"."OITM" '
        f'WHERE "ItemCode" IN ({ph})'
    )
    out, conn = {}, None
    try:
        conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                             password=h["password"], encrypt=True, sslValidateCertificate=False)
        cur = conn.cursor()
        cur.execute(sql, codes)
        for item, name in cur.fetchall():
            out[item] = name or ""
        cur.close()
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("Item-master query failed (%s)", e)
        return None
    finally:
        if conn is not None:
            conn.close()
    return out


class MarketplaceSapGateway:
    def __init__(self, company_code):
        self.company_code = company_code
        self.simulate = bool(getattr(settings, "MARKETPLACE_SIMULATE_SAP", False))
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from sap_client.client import SAPClient
            self._client = SAPClient(self.company_code)
        return self._client

    # ── stock ────────────────────────────────────────────────────────────────
    def verify_stock(self, lines, warehouse_code):
        """Raise MarketplaceError(INSUFFICIENT_STOCK) if any item lacks on-hand.

        Queries SAP OITH on-hand (via :func:`oitw_onhand`). Best-effort: a no-op in
        simulate mode and when on-hand can't be read (HANA down) — the demand is
        then treated as unknown and nothing is blocked, matching the bulk path, so
        SAP remains the final arbiter.
        """
        if self.simulate:
            return
        onhand = oitw_onhand(self.company_code, [l["item_code"] for l in lines], warehouse_code)
        if not onhand:
            return  # unknown on-hand → don't block
        # Aggregate demand per item (an item may appear on several lines).
        demand = {}
        for line in lines:
            code = line["item_code"]
            demand[code] = demand.get(code, Decimal("0")) + Decimal(line["required_quantity"])
        short = [
            {"item_code": code, "required": str(required), "available": str(onhand.get(code, Decimal("0")))}
            for code, required in demand.items()
            if onhand.get(code, Decimal("0")) < required
        ]
        if short:
            raise MarketplaceError(
                "Insufficient stock for one or more items.",
                code="INSUFFICIENT_STOCK", status_code=409, detail=short,
            )

    # ── writes ───────────────────────────────────────────────────────────────
    @staticmethod
    def _sap_write(fn, payload, *, label):
        """Run a SAP Service-Layer write, turning SAP failures into a
        :class:`MarketplaceError` so the client sees the real reason (SAP's own
        validation message) instead of an opaque HTTP 500.
        """
        from sap_client.exceptions import (
            SAPConnectionError, SAPDataError, SAPValidationError,
        )
        try:
            return fn(payload)
        except SAPValidationError as e:
            raise MarketplaceError(
                f"SAP rejected the {label}: {e}",
                code="SAP_VALIDATION", status_code=422,
            )
        except SAPConnectionError as e:
            raise MarketplaceError(
                f"Couldn't reach SAP to post the {label}: {e}",
                code="SAP_UNAVAILABLE", status_code=502,
            )
        except SAPDataError as e:
            raise MarketplaceError(
                f"SAP error while posting the {label}: {e}",
                code="SAP_ERROR", status_code=502,
            )

    @staticmethod
    def _series(series):
        """SAP expects a numeric Series id; ignore blank/non-numeric master values."""
        series = (series or "").strip()
        return int(series) if series.isdigit() else None

    @staticmethod
    def _branch(branch_id):
        """SAP expects a numeric Business Place id; ignore blank/non-numeric."""
        if branch_id in (None, ""):
            return None
        try:
            return int(branch_id)
        except (TypeError, ValueError):
            return None

    def create_delivery_note(
        self, *, ref, card_code, warehouse_code, fg_lines, doc_date,
        num_at_card="", comments="", series="", tax_code="", branch_id=None,
        ship_to_code="", pay_to_code="",
    ):
        if not fg_lines:
            return {"DocEntry": None, "DocNum": ""}
        if self.simulate:
            return {"DocEntry": 900000 + int(ref), "DocNum": f"SIMDN-{ref}"}
        items = {l["item_code"] for l in fg_lines}
        stock = self._batch_stock(items, warehouse_code)
        cost = self._cost_centers(items)  # item → cost centre ("Variety"), from history
        payload = {
            "CardCode": card_code,
            "DocDate": doc_date.isoformat(),
            # Traceability back to the marketplace order — also the key a future
            # duplicate-guard would query SAP on before re-posting.
            "NumAtCard": num_at_card or "",
            "Comments": (comments or f"Marketplace dispatch {ref}")[:254].upper(),  # SAP caps at 254 + requires UPPERCASE
            "DocumentLines": [
                self._line(l, warehouse_code, tax_code, self._alloc(l, warehouse_code, stock),
                           cost.get(l["item_code"]))
                for l in fg_lines
            ],
        }
        # Ship-to / bill-to SAP address code — sets the GST Place of Supply (and
        # hence IGST vs CGST+SGST) per the buyer's state. Branch stays as branch_id.
        if ship_to_code:
            payload["ShipToCode"] = ship_to_code
            payload["PayToCode"] = pay_to_code or ship_to_code
        sid = self._series(series)
        if sid is not None:
            payload["Series"] = sid
        bpl = self._branch(branch_id)
        if bpl is not None:
            # SAP GST branch. The Service Layer property is BPL_IDAssignedToInvoice
            # (maps to ODRF.BPLId); the plain "BPLId" key is ignored by SAP.
            payload["BPL_IDAssignedToInvoice"] = bpl
        data = self._sap_write(self.client.create_delivery_note, payload, label="delivery note")
        if data.get("pending_approval"):
            return {"DocEntry": None, "DocNum": "", "pending_approval": True,
                    "draft_entry": data.get("draft_entry")}
        return {"DocEntry": data.get("DocEntry"), "DocNum": str(data.get("DocNum") or "")}

    def create_goods_issue(
        self, *, ref, warehouse_code, pm_lines, doc_date, num_at_card="", comments="", series="",
        branch_id=None,
    ):
        if not pm_lines:
            return {"DocEntry": None, "DocNum": ""}
        if self.simulate:
            return {"DocEntry": 800000 + int(ref), "DocNum": f"SIMGI-{ref}"}
        stock = self._batch_stock({l["item_code"] for l in pm_lines}, warehouse_code)
        payload = {
            "DocDate": doc_date.isoformat(),
            "NumAtCard": num_at_card or "",
            "Comments": (comments or f"Marketplace dispatch {ref} packing-material consumption")[:254].upper(),
            "DocumentLines": [
                self._line(l, warehouse_code, "", self._alloc(l, warehouse_code, stock))
                for l in pm_lines
            ],
        }
        sid = self._series(series)
        if sid is not None:
            payload["Series"] = sid
        bpl = self._branch(branch_id)
        if bpl is not None:
            payload["BPL_IDAssignedToInvoice"] = bpl
        data = self._sap_write(self.client.create_goods_issue, payload, label="goods issue")
        if data.get("pending_approval"):
            return {"DocEntry": None, "DocNum": "", "pending_approval": True,
                    "draft_entry": data.get("draft_entry")}
        return {"DocEntry": data.get("DocEntry"), "DocNum": str(data.get("DocNum") or "")}

    # ── approval reconciliation ───────────────────────────────────────────────
    def get_delivery_note(self, doc_entry):
        """The FULL delivery note as SAP holds it, or None.

        No ``select``: the printed challan needs the header, both address blocks, the
        GST identity, the e-way bill block and every line, and naming ~40 fields
        would silently drop one the day the layout gains a row.

        Simulated mode returns a minimal document so the print view is exercisable
        without SAP (tests, local dev).
        """
        if not doc_entry:
            return None
        if self.simulate:
            return {
                "DocEntry": int(doc_entry), "DocNum": f"SIMDN-{doc_entry}",
                "DocDate": "2026-01-01T00:00:00Z", "DocTime": "10:00:00",
                "CardCode": "SIM-CARD", "CardName": "SIMULATED CUSTOMER",
                "NumAtCard": f"MKT-SIM-{doc_entry}", "DocCurrency": "INR",
                "Cancelled": "tNO", "BPLName": "SIM BRANCH",
                "BPL_IDAssignedToInvoice": 1, "DocumentLines": [],
            }
        rows = self.client.list_documents(
            "DeliveryNotes", filter=f"DocEntry eq {int(doc_entry)}", top=1)
        return rows[0] if rows else None

    def find_delivery_note_by_ref(self, num_at_card):
        """Return ``{DocEntry, DocNum}`` for a POSTED delivery note whose NumAtCard
        matches ``num_at_card`` (i.e. an approval draft that was approved and added),
        else None. Used to finalize awaiting-approval dispatches.
        """
        if self.simulate or not num_at_card:
            return None
        # num_at_card is internally generated (MKT-YYYYMMDD-<pk>), but escape the
        # OData string delimiter defensively so a stray quote can't break the filter.
        safe_ref = str(num_at_card).replace("'", "''")
        rows = self.client.list_documents(
            "DeliveryNotes",
            select="DocEntry,DocNum",
            filter=f"NumAtCard eq '{safe_ref}'",
            top=1,
        )
        if rows:
            r = rows[0]
            return {"DocEntry": r.get("DocEntry"), "DocNum": str(r.get("DocNum") or "")}
        return None

    def draft_rejected(self, draft_entry):
        """True if the approval request for ``draft_entry`` was rejected (so the
        dispatch can be re-cut). Best-effort; False on any uncertainty."""
        if self.simulate or draft_entry is None:
            return False
        rows = self.client.list_documents(
            "ApprovalRequests",
            select="Code,Status",
            filter=f"DraftEntry eq {int(draft_entry)}",
            top=1,
        )
        return bool(rows) and rows[0].get("Status") == "arsRejected"

    @staticmethod
    def _line(line, warehouse_code, tax_code, batches=None, cost_center=None):
        row = {
            "ItemCode": line["item_code"],
            "Quantity": float(Decimal(line["required_quantity"])),
            "WarehouseCode": line.get("warehouse_code") or warehouse_code,
            # Line UDFs the custom SAP validation requires on a sales delivery.
            "U_Purpose": "SALE",
            "U_UNE_SCHI": "N",
            "U_UNE_CUNT": "Y",
        }
        if tax_code:
            row["VatGroup"] = tax_code  # per-line tax (GST) from the warehouse master
        if batches:
            row["BatchNumbers"] = batches  # batch-managed items need explicit batches
        if cost_center:
            # "Variety" — the cost centre (OcrCode) SAP requires on every line.
            row["CostingCode"] = cost_center
            row["U_SchemeAgst"] = cost_center
        return row

    def _cost_centers(self, item_codes):
        """``{item_code: cost_centre}`` — each item's most-used cost centre
        ("Variety"/OcrCode) from its delivery-note + invoice history. Best-effort;
        {} if HANA is unavailable."""
        codes = [c for c in {(c or "").strip() for c in item_codes} if c]
        if not codes:
            return {}
        try:
            from hdbcli import dbapi
            from sap_client.context import CompanyContext
            h = CompanyContext(self.company_code).hana
        except Exception as e:  # pragma: no cover - env specific
            logger.warning("Cost-centre lookup unavailable (%s)", e)
            return {}
        ph = ",".join(["?"] * len(codes))
        sql = (
            f'SELECT "ItemCode","OcrCode",COUNT(*) c FROM ('
            f'  SELECT "ItemCode","OcrCode" FROM "{h["schema"]}"."DLN1" '
            f'   WHERE "ItemCode" IN ({ph}) AND "OcrCode" IS NOT NULL AND "OcrCode"!=\'\''
            f'  UNION ALL SELECT "ItemCode","OcrCode" FROM "{h["schema"]}"."INV1" '
            f'   WHERE "ItemCode" IN ({ph}) AND "OcrCode" IS NOT NULL AND "OcrCode"!=\'\') '
            f'GROUP BY "ItemCode","OcrCode" ORDER BY "ItemCode",c DESC'
        )
        out, conn = {}, None
        try:
            conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                                 password=h["password"], encrypt=True, sslValidateCertificate=False)
            cur = conn.cursor()
            cur.execute(sql, [*codes, *codes])
            for item, ocr, _cnt in cur.fetchall():
                out.setdefault(item, ocr)  # first = most-used (ordered by count desc)
            cur.close()
        except Exception as e:  # pragma: no cover - env specific
            logger.warning("Cost-centre query failed (%s)", e)
            return {}
        finally:
            if conn is not None:
                conn.close()
        return out

    # ── batch selection ───────────────────────────────────────────────────────
    def _batch_stock(self, item_codes, warehouse_code):
        """``{item_code: [(batch, qty), …]}`` — on-hand batches for the given items
        in a warehouse, oldest first (FIFO). Empty for non-batch items. Best-effort:
        returns {} if HANA is unavailable (the document add then reports the real
        SAP error)."""
        codes = [c for c in {(c or "").strip() for c in item_codes} if c]
        if not codes or not warehouse_code:
            return {}
        try:
            from hdbcli import dbapi
            from sap_client.context import CompanyContext
            h = CompanyContext(self.company_code).hana
        except Exception as e:  # pragma: no cover - env specific
            logger.warning("Batch stock lookup unavailable (%s)", e)
            return {}
        placeholders = ",".join(["?"] * len(codes))
        sql = (
            f'SELECT "ItemCode","BatchNum","Quantity" FROM "{h["schema"]}"."OIBT" '
            f'WHERE "WhsCode"=? AND "Quantity">0 AND "ItemCode" IN ({placeholders}) '
            f'ORDER BY "ItemCode","InDate","BatchNum"'
        )
        out = {}
        conn = None
        try:
            conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                                 password=h["password"], encrypt=True, sslValidateCertificate=False)
            cur = conn.cursor()
            cur.execute(sql, [warehouse_code, *codes])
            for item, batch, qty in cur.fetchall():
                out.setdefault(item, []).append((batch, Decimal(str(qty))))
            cur.close()
        except Exception as e:  # pragma: no cover - env specific
            logger.warning("Batch stock query failed (%s)", e)
            return {}
        finally:
            if conn is not None:
                conn.close()
        return out

    def _alloc(self, line, warehouse_code, stock):
        """Allocate a line's quantity across available batches (FIFO). Returns a
        BatchNumbers list, or None when the item has no batch stock (not
        batch-managed). Raises INSUFFICIENT_BATCH when stock can't cover it."""
        item = line["item_code"]
        batches = stock.get(item) or stock.get((item or "").strip())
        if not batches:
            return None
        remaining = Decimal(line["required_quantity"])
        alloc = []
        for batch, bqty in batches:
            if remaining <= 0:
                break
            take = min(remaining, bqty)
            if take > 0:
                alloc.append({"BatchNumber": batch, "Quantity": float(take)})
                remaining -= take
        if remaining > 0:
            raise MarketplaceError(
                f"Not enough batch stock for {item} in "
                f"{line.get('warehouse_code') or warehouse_code} (short by {remaining}).",
                code="INSUFFICIENT_BATCH", status_code=409,
            )
        return alloc
