from django.conf import settings
from .exceptions import SAPValidationError


COMPANY_SAP_REGISTRY = {
    "JIVO_OIL": {
        "hana": {
            "host": settings.HANA_HOST,
            "port": settings.HANA_PORT,
            "user": settings.HANA_USER,
            "password": settings.HANA_PASSWORD,
            "schema": settings.COMPANY_DB["JIVO_OIL"],
        },
        "service_layer": {
            "base_url": settings.SL_URL,
            "company_db": settings.COMPANY_DB["JIVO_OIL"],
            "username": settings.SL_USER,
            "password": settings.SL_PASSWORD,
            "approval_username": settings.SAP_APPROVAL_USER,
            "approval_password": settings.SAP_APPROVAL_PASSWORD,
        }
    },
    "JIVO_MART": {
        "hana": {
            "host": settings.HANA_HOST,
            "port": settings.HANA_PORT,
            "user": settings.HANA_USER,
            "password": settings.HANA_PASSWORD,
            "schema": settings.COMPANY_DB["JIVO_MART"],
        },
        "service_layer": {
            "base_url": settings.SL_URL,
            "company_db": settings.COMPANY_DB["JIVO_MART"],
            "username": settings.SL_USER,
            "password": settings.SL_PASSWORD,
            "approval_username": settings.SAP_APPROVAL_USER,
            "approval_password": settings.SAP_APPROVAL_PASSWORD,
        }
    },
    "JIVO_BEVERAGES": {
        "hana": {
            "host": settings.HANA_HOST,
            "port": settings.HANA_PORT,
            "user": settings.HANA_USER,
            "password": settings.HANA_PASSWORD,
            "schema": settings.COMPANY_DB["JIVO_BEVERAGES"],
        },
        "service_layer": {
            "base_url": settings.SL_URL,
            "company_db": settings.COMPANY_DB["JIVO_BEVERAGES"],
            "username": settings.SL_USER,
            "password": settings.SL_PASSWORD,
            "approval_username": settings.SAP_APPROVAL_USER,
            "approval_password": settings.SAP_APPROVAL_PASSWORD,
        }
    }
}


def get_company_config(company_code: str) -> dict:
    try:
        return COMPANY_SAP_REGISTRY[company_code]
    except KeyError:
        raise SAPValidationError(f"Invalid company code: {company_code}")
