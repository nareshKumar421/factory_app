from django.apps import AppConfig


class FixedAssetGrpoConfig(AppConfig):
    """Fixed Asset GRPO: late PO link and SAP GRPO for fixed-asset gate entries.

    Owns its two tables (closure, record), the late PO link onto
    raw_material_gatein's receipt tables, and its own SAP posting queue kind.
    It edits no other app: the queue handler is registered here, at start-up,
    into ``sap_postings.services`` with ``setdefault``, so a kind someone else
    registered under the same name would win rather than be overwritten.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "fixed_asset_grpo"
    verbose_name = "Fixed Asset GRPO"

    def ready(self):
        from sap_postings import services as sap_postings

        from .constants import HANDLER_PATH, KIND, KIND_LABEL, KIND_SHORT

        sap_postings.HANDLERS.setdefault(KIND, HANDLER_PATH)
        sap_postings.KIND_LABELS.setdefault(KIND, KIND_LABEL)
        sap_postings.KIND_SHORT.setdefault(KIND, KIND_SHORT)
