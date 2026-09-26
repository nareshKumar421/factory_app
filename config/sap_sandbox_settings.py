"""Settings for proving SAP writes against the sandbox company, never live data.

SAP Portal ran a second instance (``npm run start:test`` with ``.env.test``)
pinned to ``TEST_JIVO_OIL_HANADB``, the sandbox copy of Jivo Oil, so posting
paths could be tried end to end. This is JI's equivalent for the writers the
merge added (business partners, BOMs, budgets, production-order status,
approval withdraw) and any other Service Layer write.

* Database: the local ``factory_local`` PostgreSQL from ``local_dev_settings``
  — never production.
* SAP: the real HANA / Service Layer hosts from ``.env`` (so do NOT source
  ``offline.env`` with this module), but ``JIVO_OIL`` resolves to the sandbox
  company. ``JIVO_MART`` and ``JIVO_BEVERAGES`` resolve to a name SAP refuses,
  so a request made under either company fails instead of touching live data.

The portal's note on the sandbox: the Service Layer works there, but direct
HANA reads need a grant on the sandbox schema for JI's HANA user.

    python manage.py runserver 8001 --settings=config.sap_sandbox_settings
"""
from .local_dev_settings import *  # noqa: F401,F403

SAP_SANDBOX_COMPANY_DB = "TEST_JIVO_OIL_HANADB"

COMPANY_DB = {
    "JIVO_OIL": SAP_SANDBOX_COMPANY_DB,
    "JIVO_MART": "SANDBOX_HAS_NO_JIVO_MART",
    "JIVO_BEVERAGES": "SANDBOX_HAS_NO_JIVO_BEVERAGES",
}
