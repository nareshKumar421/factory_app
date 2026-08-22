"""
sap_reports/exceptions.py

Errors raised while cataloguing or running a SAP saved query. SAP connectivity
and data problems keep using ``sap_client.exceptions`` so every module surfaces
them the same way.
"""


class SapReportError(Exception):
    """Base class for problems with a SAP report."""


class SapReportSqlError(SapReportError):
    """The saved query cannot be run: not read-only, or a prompt is unfilled."""


class SapReportParameterError(SapReportError):
    """A parameter value supplied by the caller is missing or malformed."""
