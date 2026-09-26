"""Create business partners (customers and vendors) in SAP.

Ported from SAP Portal's ``createCustomer`` / ``createVendor``
(``backend_v1/services/sapServiceLayer.js``). The payload is built by the app
that owns the registration (``partner_onboarding.services``); this writer only
posts it and reports what SAP created.

SAP has no idempotency key and accepts two partners with the same GSTIN, so a
caller must lock its own row and ask SAP first
(``SAPClient.business_partner`` / ``partners_with_tax_ids``) — the portal did
neither and created duplicates whenever an approval was clicked twice.
"""

import logging

from .entity_client import ServiceLayerEntityClient

logger = logging.getLogger(__name__)

# Room for the company's SBO_SP_TransactionNotification on add.
POST_TIMEOUT_SECONDS = 120


class BusinessPartnerWriter:
    """POST /b1s/v2/BusinessPartners."""

    def __init__(self, context):
        self.context = context

    def create(self, payload: dict) -> dict:
        data = ServiceLayerEntityClient(self.context).post(
            "BusinessPartners",
            payload,
            timeout=POST_TIMEOUT_SECONDS,
            label=f"create business partner {payload.get('CardCode') or ''}".strip(),
        )
        card_code = data.get("CardCode") or payload.get("CardCode")
        logger.info("Business partner %s created in SAP", card_code)
        return {
            "card_code": card_code,
            "card_name": data.get("CardName") or payload.get("CardName"),
            "attachment_entry": data.get("AttachmentEntry"),
        }
