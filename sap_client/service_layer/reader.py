"""Generic read helper for the SAP Service Layer (OData collections).

Used for lightweight lookups (e.g. finding a posted Delivery Note by NumAtCard
when reconciling approval drafts). Modelled on the writers' session handling.
"""
import logging

import requests

from ..exceptions import SAPConnectionError
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)


def list_collection(context, entity: str, *, select: str = "", filter: str = "",
                    top: int = 20) -> list:
    """GET ``/b1s/v2/{entity}`` with optional ``$select``/``$filter``/``$top``.

    Returns the OData ``value`` list. Raises :class:`SAPConnectionError` if the
    Service Layer is unreachable; returns [] on a non-200 response.
    """
    sl = context.service_layer
    try:
        cookies = ServiceLayerSession(sl).login()
    except requests.exceptions.RequestException as e:
        logger.error(f"Service Layer login failed for read: {e}")
        raise SAPConnectionError("Unable to connect to SAP Service Layer")

    params = {}
    if select:
        params["$select"] = select
    if filter:
        params["$filter"] = filter
    if top:
        params["$top"] = top

    url = f"{sl['base_url']}/b1s/v2/{entity}"
    try:
        r = requests.get(url, params=params, cookies=cookies, timeout=30, verify=False)
    except requests.exceptions.RequestException as e:
        logger.error(f"Service Layer read of {entity} failed: {e}")
        raise SAPConnectionError("SAP Service Layer request failed")
    if r.status_code != 200:
        logger.warning(f"Service Layer read of {entity} returned {r.status_code}: {r.text[:200]}")
        return []
    return r.json().get("value", []) or []
