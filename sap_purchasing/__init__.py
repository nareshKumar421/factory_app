"""SAP purchasing plumbing for the imprest (cash book -> SAP) and Fixed Asset
GRPO work, kept apart from ``sap_client`` so nothing there changes.

A plain package: no models, no migrations, not in INSTALLED_APPS. It only
imports ``sap_client`` (context, exceptions, Service Layer and HANA helpers)
and never edits it. ``cash_sap`` and ``fixed_asset_grpo`` code against the
modules here; see ``README.md`` for the public API.

Nothing is imported at package level on purpose: ``sap_client.registry`` reads
Django settings at import time, so each module is imported where it is used.
"""
