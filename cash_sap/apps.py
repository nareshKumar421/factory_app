from django.apps import AppConfig


class CashSapConfig(AppConfig):
    """Cash book -> SAP: imprest PO per bunch, GRPO per voucher, A/P drafts.

    Posts only through ``sap_postings``. Its four kinds are registered here, with
    ``setdefault``, because ``sap_postings/services.py`` is not edited on this
    branch: a kind already registered there (by a later merge) wins.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "cash_sap"
    verbose_name = "Cash book to SAP"

    def ready(self):
        from sap_postings import services as sap_postings

        from .sap_posting import HANDLERS, KIND_LABELS, KIND_SHORT

        for kind, path in HANDLERS.items():
            sap_postings.HANDLERS.setdefault(kind, path)
        for kind, label in KIND_LABELS.items():
            sap_postings.KIND_LABELS.setdefault(kind, label)
        for kind, short in KIND_SHORT.items():
            sap_postings.KIND_SHORT.setdefault(kind, short)
