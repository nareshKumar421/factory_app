"""
Import SAP Portal's vendor registrations (``ZVENDOR_PORTAL``) from a JSON export.

    python manage.py import_portal_vendors --from-file zvendor_portal.json --dry-run
    python manage.py import_portal_vendors --from-file zvendor_portal.json --actor ops@example.com --yes

Options, behaviour and the export SELECT: ``_portal_import.py`` and
``partner_onboarding/docs/README.md``.
"""

from partner_onboarding.families import VENDOR_FAMILY

from ._portal_import import PortalImportCommand


class Command(PortalImportCommand):
    help = "Import SAP Portal's vendor registrations (ZVENDOR_PORTAL) from a JSON export."
    family = VENDOR_FAMILY
