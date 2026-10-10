"""Posting to SAP as the person, not as the shared integration account.

SAP's TransactionNotification checks the posting user (``UserSign``): which
users may enter which kinds of order, who is exempt from the sales team's
approval. The integration account ``B1i`` is on none of those lists, and SAP
should record who actually did the work. So every step here logs into the
Service Layer as the SAP account mapped to the person who took it
(``sap_client.SapApproverIdentity``), with that account's password from the
server's ``SAP_APPROVER_CREDENTIALS`` map. Passwords are never stored in the
database or sent to the browser.
"""

from django.conf import settings

from sap_client.context import CompanyContext
from sap_client.models import SapApproverIdentity
from sap_client.service_layer.entity_client import ServiceLayerEntityClient


class NoSapLogin(Exception):
    """The person cannot post to SAP from the app yet. Says what is missing."""


def sap_user_code(user, company) -> str | None:
    return SapApproverIdentity.code_for(user, company)


def _password(company, code: str) -> str:
    return (settings.SAP_APPROVER_CREDENTIALS.get(company.code) or {}).get(code) or ""


def login_status(user, company) -> dict:
    """What the screens show about the person's SAP login, without the secret."""
    code = sap_user_code(user, company)
    if not code:
        return {
            "sap_user_code": "",
            "ready": False,
            "message": (
                f"You have no SAP user linked for {company.name}. Ask an administrator to link "
                "yours on SAP Identities."
            ),
        }
    if not _password(company, code):
        return {
            "sap_user_code": code,
            "ready": False,
            "message": (
                f"The server does not hold the SAP password for {code} in {company.name} yet. "
                "Ask the administrator to add it."
            ),
        }
    return {"sap_user_code": code, "ready": True, "message": ""}


def sap_client_for(user, company) -> ServiceLayerEntityClient:
    """A Service Layer client logged in as ``user``'s own SAP account."""
    status = login_status(user, company)
    if not status["ready"]:
        raise NoSapLogin(status["message"])
    code = status["sap_user_code"]
    context = CompanyContext(company.code)
    # A copy: ``context.service_layer`` is the shared registry dict.
    config = dict(context.service_layer, username=code, password=_password(company, code))
    return ServiceLayerEntityClient(context, config)
