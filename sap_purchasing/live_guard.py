"""Refuse to write into live SAP unless someone switched that on.

The imprest and Fixed Asset GRPO postings are new, and the first weeks of them
belong on the ``TEST_`` copies of the company databases. The worker already has
a guard for this (``run_sap_postings --require-test-sap``), but it reads
``settings.COMPANY_DB`` -- and ``sap_client.registry.COMPANY_SAP_REGISTRY`` is
a snapshot of that dict taken at import. A test that overrides the setting, or
a settings change after start-up, would make the two disagree, and the guard
would then be checking a database the writers do not use.

So this guard reads the two databases the writers and readers **actually**
talk to, straight from the company's ``CompanyContext``:

* ``service_layer["company_db"]`` -- the database every Service Layer login
  names (``ServiceLayerSession.login``), so every document write;
* ``hana["schema"]`` -- the schema every read-back and series query names.

Both must start with ``TEST_`` (case-insensitive, the same rule as
``--require-test-sap``) for the company to count as a test target. A blank
value is not ``TEST_`` and so counts as live: the guard fails closed.

Order of checks, binding for every caller (spec D8): **simulate is checked
before this guard**. A simulated send writes nothing, so a dry run works on any
server; only a real send has to pass here. The switches are the callers' own
(``CASH_SAP_ALLOW_LIVE``, ``FIXED_ASSET_GRPO_ALLOW_LIVE``), both default False,
and are passed in as ``allow_live`` so this module reads no settings itself.

An unknown company code raises ``SAPValidationError`` from the registry
(``get_company_config``). Callers treat that as a rejection, the same as the
``ValueError`` below.
"""

from typing import Tuple

from sap_client.context import CompanyContext

TEST_PREFIX = "TEST_"


def target_databases(company_code: str) -> Tuple[str, str]:
    """``(service_layer_company_db, hana_schema)`` the company's writers use.

    Read through ``CompanyContext`` on every call -- never cached -- so a test
    that patches ``sap_purchasing.live_guard.CompanyContext`` sees its own
    values, and so does a process whose registry differs from settings.
    """
    ctx = CompanyContext(company_code)
    return (
        str(ctx.service_layer.get("company_db") or ""),
        str(ctx.hana.get("schema") or ""),
    )


def is_test_target(company_code: str) -> bool:
    """True when both the Service Layer database and the HANA schema are TEST_.

    The same rule as ``run_sap_postings --require-test-sap``
    (``db.upper().startswith("TEST_")``), applied to both databases because a
    write goes through one and its read-back through the other: a TEST_ Service
    Layer with a live HANA schema would post to test and then look for the
    document in live, and never find it.
    """
    return all(db.upper().startswith(TEST_PREFIX) for db in target_databases(company_code))


def require_test_target(company_code: str, *, allow_live: bool, switch_name: str) -> None:
    """Raise ``ValueError`` unless the company is a TEST_ target or live is allowed.

    ``allow_live`` is the caller's switch value; ``switch_name`` is its setting
    name, quoted in the message so whoever reads the refusal knows exactly what
    to turn on. Returns None when the send may go ahead.
    """
    if allow_live:
        return
    if is_test_target(company_code):
        return
    sl_db, schema = target_databases(company_code)
    live = ", ".join(sorted({db or "(blank)" for db in (sl_db, schema)
                             if not db.upper().startswith(TEST_PREFIX)}))
    raise ValueError(
        f"{company_code} is live SAP ({live}) and {switch_name} is off: "
        f"only TEST_ company databases can be posted to."
    )
