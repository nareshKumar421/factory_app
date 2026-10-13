"""Service Layer writers for the imprest purchasing documents.

Three documents and two actions, all posted into one SAP company named by its
code:

* ``PurchaseOrders``          -- the imprest PO, one per (bunch, SAP company);
* ``PurchaseDeliveryNotes``   -- the imprest GRPO against that PO;
* ``Drafts`` with ``DocObjectCode=oPurchaseInvoices`` -- the A/P invoice draft
  head office opens in JSAP, checks and adds;
* ``PurchaseOrders(n)/Close`` -- after "Leave out after the PO";
* ``DraftsService_SaveDraftToDocument`` -- adding a PO or GRPO that SAP held in
  an approval procedure, once it is approved.

Why not the existing writers
----------------------------
``sap_client.service_layer.purchase_order_writer.PurchaseOrderWriter`` (the
planning PO) talks to ``/b1s/v1``, maps 401/403 to a validation error, refuses
lines without an ``ItemCode`` and never notices an approval draft.
``GRPOWriter`` does not notice approval drafts either: a GRPO that SAP routed to
approval comes back as ``404 + Location: .../Drafts(N)`` and is recorded as a
failure, and the retry makes a second draft. Neither file may change in this
branch, so these writers subclass the one family that does handle drafts --
``delivery_note_writer._ServiceLayerDocWriter`` -- and keep its session and
draft helpers, with three differences:

1. **Longer timeouts.** The base waits 30s. A purchasing document runs JIVO's
   ``SBO_SP_TransactionNotification`` with dozens of checks, so the PO and the
   draft get 120s and the GRPO 180s (the same as ``GRPOWriter``).
2. **504 means "may have committed".** The base calls every non-401/403
   failure a data error. A gateway timeout can follow a commit, so 504 here is
   ``SAPOutcomeUnknown`` (read back before trying again), and 502/503 are
   ``SAPUnavailable`` (the request never got to the company database).
3. **A 201 that cannot be read is also "may have committed".** SAP made the
   document; we just do not know its number. ``SAPOutcomeUnknown`` makes the
   caller read it back by its marker instead of posting again.

Error mapping (every write here)
--------------------------------
=====================================  ==========================
SAP answer                             Raised
=====================================  ==========================
201                                    returns ``response.json()``
404/any + ``Location: .../Drafts(N)``  returns the approval-draft shape (PO, GRPO)
400                                    ``SAPValidationError``
401, 403, 502, 503                     ``SAPUnavailable``
504                                    ``SAPOutcomeUnknown``
anything else                          ``SAPDataError``
connect timeout / refused / TLS        ``SAPUnavailable`` (``errors.never_sent``)
read timeout / dropped mid-answer      ``SAPOutcomeUnknown``
any other ``RequestException``         ``SAPOutcomeUnknown`` (e.g. ``ChunkedEncodingError``)
login failed                           ``SAPUnavailable``
=====================================  ==========================

The approval-draft shape is the base's own:
``{"DocEntry": None, "DocNum": "", "pending_approval": True, "draft_entry": N}``.
``is_pending_approval(result)`` tests for it.

Every function at the bottom takes a ``company_code`` and builds the
``CompanyContext`` itself, so a handler running for Oil can write into
Beverages by naming it. None of them reads a setting or a simulate flag: the
callers decide simulate and run ``live_guard`` before they get here.
"""

import logging
from datetime import date, datetime
from decimal import Decimal

import requests

from sap_client.context import CompanyContext
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPOutcomeUnknown,
    SAPUnavailable,
    SAPValidationError,
)
from sap_client.service_layer.delivery_note_writer import _ServiceLayerDocWriter
from sap_client.service_layer.errors import never_sent, unanswered
from sap_client.service_layer.transfer_base import TransferDocumentWriter

logger = logging.getLogger(__name__)

#: SAP object codes (ObjType) of the documents written here.
OBJ_PURCHASE_ORDER = "22"
OBJ_GRPO = "20"
OBJ_AP_INVOICE = "18"

#: Status codes that mean the request never reached the company database.
UNAVAILABLE_STATUSES = (401, 403, 502, 503)
#: Status codes after which SAP may still have committed the document.
OUTCOME_UNKNOWN_STATUSES = (504,)


def json_native(obj):
    """Decimals to float, dates to ISO strings, tuples to lists -- recursively.

    The cash_sap payload builders already return JSON-native values; this is
    the last line of defence so a stray ``Decimal`` or ``date`` cannot make
    ``requests`` raise ``TypeError`` after the login, which would read as an
    unexpected error rather than a refused payload.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.date().isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: json_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_native(v) for v in obj]
    return obj


def is_pending_approval(result) -> bool:
    """True when a create answered "held in approval" rather than "posted".

    That answer carries no DocEntry: the document is an ODRF draft whose
    number is ``result["draft_entry"]``. Store it, report IN_APPROVAL, and
    settle it later with ``readback.find_draft_by_marker`` (spec D10).
    """
    return bool(isinstance(result, dict) and result.get("pending_approval"))


class _PurchasingDocWriter(_ServiceLayerDocWriter):
    """POST one purchasing document; see the module docstring for the mapping.

    Subclasses set ``endpoint``, ``label``, ``DOC_OBJECT_CODE`` and
    ``timeout``. ``detect_approval_drafts`` is False only for the ``Drafts``
    endpoint, where a created draft *is* the success answer.
    """

    timeout = 120
    detect_approval_drafts = True

    # -- error text ----------------------------------------------------------

    def _extract_error_message(self, response) -> str:
        """Readable SAP error text, v1 or v2 shape, with the SAP code appended.

        ``TransferDocumentWriter._error_message`` is the one reader in
        sap_client that understands both shapes; it is a static method, so it
        is borrowed rather than copied.
        """
        return TransferDocumentWriter._error_message(response)

    def _raise_for_failure(self, response, what: str):
        """Turn a non-success answer into the SAP error the caller acts on."""
        message = self._extract_error_message(response)
        status = response.status_code
        if status == 400:
            logger.error("SAP refused %s: %s", what, message)
            raise SAPValidationError(message)
        if status in UNAVAILABLE_STATUSES:
            logger.error("SAP unavailable (HTTP %s) for %s: %s", status, what, message)
            raise SAPUnavailable(
                f"SAP Service Layer could not take the {self.label} (HTTP {status}). "
                f"Nothing was posted; try again."
            )
        if status in OUTCOME_UNKNOWN_STATUSES:
            logger.error("SAP gateway timeout (HTTP %s) for %s", status, what)
            raise SAPOutcomeUnknown(
                f"SAP's gateway timed out while posting the {self.label}. It may "
                f"still have been created -- check SAP before posting again."
            )
        logger.error("SAP error (HTTP %s) for %s: %s", status, what, message)
        raise SAPDataError(f"Failed to create the {self.label} in SAP: {message}")

    def _post(self, url: str, body, timeout: int, what: str):
        """POST with the transport-failure split; returns the response."""
        cookies = self._get_session_cookies()
        try:
            return requests.post(
                url,
                json=body,
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
                verify=False,
            )
        except requests.exceptions.Timeout as e:
            # ConnectTimeout is both a Timeout and a ConnectionError and lands
            # here; it never reached SAP, so it must not say "may have been
            # done" (the transfer_base._action split).
            if never_sent(e):
                logger.error("Connect timeout while %s: %s", what, e)
                raise SAPUnavailable("Unable to connect to SAP Service Layer.") from e
            logger.error("Timeout (%ss) while %s: %s", timeout, what, e)
            raise SAPOutcomeUnknown(
                f"SAP did not answer within {timeout}s while {what}. It may still "
                f"have been done -- check SAP before trying again."
            ) from e
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error while %s: %s", what, e)
            raise unanswered(e, "Unable to connect to SAP Service Layer.") from e
        except requests.exceptions.RequestException as e:
            # Anything else -- ChunkedEncodingError when the answer is cut off,
            # ContentDecodingError -- happened after SAP had the request, so it
            # may have committed: read back by the marker, do not post again.
            logger.error("SAP answer broke off while %s: %s", what, e)
            raise SAPOutcomeUnknown(
                f"SAP's answer broke off while {what}. It may still have been "
                f"done -- check SAP before trying again."
            ) from e

    # -- operations ----------------------------------------------------------

    def create(self, payload: dict) -> dict:
        """POST ``/b1s/v2/{endpoint}``. Returns SAP's created document.

        For PO and GRPO, a document SAP routed into an approval procedure
        returns ``{"DocEntry": None, "DocNum": "", "pending_approval": True,
        "draft_entry": N}`` instead (the ``Location`` header is checked before
        the status code, as the base does).
        """
        url = f"{self.sl_config['base_url']}/b1s/v2/{self.endpoint}"
        response = self._post(url, json_native(payload), self.timeout,
                              f"posting the {self.label}")

        if response.status_code == 201:
            try:
                created = response.json()
            except ValueError as e:
                raise SAPOutcomeUnknown(
                    f"SAP created the {self.label} but its answer could not be "
                    f"read. Read it back before posting again."
                ) from e
            logger.info("SAP %s created: DocEntry=%s DocNum=%s",
                        self.label, created.get("DocEntry"), created.get("DocNum"))
            return created

        if self.detect_approval_drafts:
            draft_entry = self._approval_draft_entry(response)
            if draft_entry is not None:
                logger.info("SAP %s routed to approval draft %s", self.label, draft_entry)
                return {"DocEntry": None, "DocNum": "",
                        "pending_approval": True, "draft_entry": draft_entry}

        self._raise_for_failure(response, f"the {self.label}")

    def save_draft_to_document(self, draft_entry: int) -> None:
        """Add an approved draft as the real document (``DraftsService_SaveDraftToDocument``).

        Overrides the base only for the error mapping and the timeout: the add
        is a full document post, notification procedure and all, so it gets
        this writer's timeout and the 502/503/504 split. SAP answers 200/204
        with no body; read the posted document back by marker or by
        ``draftKey`` (``readback.find_by_draft_key``).
        """
        if not self.DOC_OBJECT_CODE:
            raise SAPDataError(f"The {self.label} writer cannot add drafts.")
        url = f"{self.sl_config['base_url']}/b1s/v2/DraftsService_SaveDraftToDocument"
        body = {"Document": {"DocEntry": int(draft_entry),
                             "DocObjectCode": self.DOC_OBJECT_CODE}}
        what = f"adding {self.label} draft {draft_entry}"
        response = self._post(url, body, self.timeout, what)
        if response.status_code in (200, 204):
            logger.info("SAP %s draft %s added as a document", self.label, draft_entry)
            return
        self._raise_for_failure(response, what)


class PurchaseOrderDocWriter(_PurchasingDocWriter):
    """``POST /b1s/v2/PurchaseOrders`` (OPOR) and ``PurchaseOrders(n)/Close``."""

    endpoint = "PurchaseOrders"
    label = "Purchase Order"
    DOC_OBJECT_CODE = "oPurchaseOrders"
    timeout = 120
    close_timeout = 60

    def close(self, doc_entry: int) -> None:
        """Close the PO's open remainder. 200/204 -> None.

        Closing is idempotent in effect but not in answer: SAP refuses to close
        a closed PO with a 400. Callers read ``document_state`` first and skip
        the call when ``doc_status == "C"``.
        """
        url = f"{self.sl_config['base_url']}/b1s/v2/PurchaseOrders({int(doc_entry)})/Close"
        what = f"closing Purchase Order {doc_entry}"
        response = self._post(url, None, self.close_timeout, what)
        if response.status_code in (200, 204):
            logger.info("SAP Purchase Order DocEntry=%s closed", doc_entry)
            return
        self._raise_for_failure(response, what)


class PurchaseDeliveryNoteDocWriter(_PurchasingDocWriter):
    """``POST /b1s/v2/PurchaseDeliveryNotes`` (OPDN): the imprest GRPO."""

    endpoint = "PurchaseDeliveryNotes"
    label = "GRPO"
    DOC_OBJECT_CODE = "oPurchaseDeliveryNotes"
    timeout = 180


class PurchaseInvoiceDraftWriter(_PurchasingDocWriter):
    """``POST /b1s/v2/Drafts`` with ``DocObjectCode=oPurchaseInvoices`` (ODRF, ObjType 18).

    A 201 is the draft itself: its ``DocEntry`` is the **draft** entry, not an
    OPCH entry. There is no approval detection on this endpoint -- nothing is
    posted, so nothing is held. Head office adds the draft in JSAP.
    """

    endpoint = "Drafts"
    label = "A/P invoice draft"
    DOC_OBJECT_CODE = "oPurchaseInvoices"
    timeout = 120
    detect_approval_drafts = False

    def create(self, payload: dict) -> dict:
        """Inject ``DocObjectCode`` (a copy; the caller's dict is not changed).

        Any other ``DocObjectCode`` is refused with ``SAPValidationError``
        before anything is sent: this writer must never make some other kind
        of draft.
        """
        payload = dict(payload)
        payload.setdefault("DocObjectCode", self.DOC_OBJECT_CODE)
        if payload["DocObjectCode"] != self.DOC_OBJECT_CODE:
            raise SAPValidationError(
                f"The A/P draft writer only makes {self.DOC_OBJECT_CODE} drafts, "
                f"not {payload['DocObjectCode']}."
            )
        return super().create(payload)

    def save_draft_to_document(self, draft_entry: int) -> None:
        raise SAPDataError(
            "A/P invoice drafts are added by head office in SAP, not by this app."
        )


#: Object code -> writer class, for ``save_draft_to_document``.
DRAFT_WRITERS = {
    OBJ_PURCHASE_ORDER: PurchaseOrderDocWriter,
    OBJ_GRPO: PurchaseDeliveryNoteDocWriter,
}


# ---------------------------------------------------------------------------
# Public functions: one company code in, one SAP call out
# ---------------------------------------------------------------------------

def create_purchase_order(company_code: str, payload: dict) -> dict:
    """POST an imprest PO into ``company_code``. Created JSON, or the approval-draft shape."""
    return PurchaseOrderDocWriter(CompanyContext(company_code)).create(payload)


def close_purchase_order(company_code: str, doc_entry: int) -> None:
    """``PurchaseOrders(doc_entry)/Close`` in ``company_code``."""
    PurchaseOrderDocWriter(CompanyContext(company_code)).close(doc_entry)


def create_purchase_delivery_note(company_code: str, payload: dict) -> dict:
    """POST an imprest GRPO into ``company_code``. Created JSON, or the approval-draft shape."""
    return PurchaseDeliveryNoteDocWriter(CompanyContext(company_code)).create(payload)


def create_purchase_invoice_draft(company_code: str, payload: dict) -> dict:
    """POST an A/P invoice draft into ``company_code``; ``result["DocEntry"]`` is the draft entry."""
    return PurchaseInvoiceDraftWriter(CompanyContext(company_code)).create(payload)


def save_draft_to_document(company_code: str, object_code: str, draft_entry: int) -> None:
    """Add an approved PO ("22") or GRPO ("20") draft as the real document.

    Any other object code raises ``ValueError`` -- A/P drafts are head
    office's to add.
    """
    writer_class = DRAFT_WRITERS.get(str(object_code))
    if writer_class is None:
        raise ValueError(
            f"Only purchase order (22) and GRPO (20) drafts can be added here, not {object_code}."
        )
    writer_class(CompanyContext(company_code)).save_draft_to_document(draft_entry)


__all__ = [
    "OBJ_PURCHASE_ORDER", "OBJ_GRPO", "OBJ_AP_INVOICE",
    "PurchaseOrderDocWriter", "PurchaseDeliveryNoteDocWriter", "PurchaseInvoiceDraftWriter",
    "create_purchase_order", "close_purchase_order", "create_purchase_delivery_note",
    "create_purchase_invoice_draft", "save_draft_to_document",
    "is_pending_approval", "json_native",
    # re-exported so callers can catch everything from one place
    "SAPConnectionError", "SAPDataError", "SAPOutcomeUnknown", "SAPUnavailable",
    "SAPValidationError",
]
