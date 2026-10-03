import logging
from typing import List

from hdbcli import dbapi

from .connection import HanaConnection
from ..dtos import VendorDTO
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaVendorReader:
    """Reads vendors from SAP HANA.

    When HANA cannot be reached, the active vendor list comes from the nightly
    copy (``sap_mirror``, list ``vendors``), so the gate can still pick the
    supplier of a truck. ``use_copy=False`` is for the job that takes the copy.
    """

    def __init__(self, context, *, use_copy: bool = True):
        self.connection = HanaConnection(context.hana)
        company_code = getattr(context, "company_code", None)
        self._copy_company = company_code if use_copy and isinstance(company_code, str) else None

    def get_active_vendors(self) -> List[VendorDTO]:
        try:
            return self._active_vendors_live()
        except (SAPConnectionError, SAPDataError) as exc:
            from sap_mirror import services as sap_mirror

            if not self._copy_company or not sap_mirror.hana_unreachable(exc):
                raise
            try:
                copy = sap_mirror.copied_rows(self._copy_company, sap_mirror.VENDORS)
            except Exception:  # noqa: BLE001 -- a broken copy must not change what the caller sees
                logger.exception("SAP vendor copy could not answer for %s", self._copy_company)
                raise exc
            if copy is None:
                raise
            logger.warning("HANA unreachable; vendors for %s answered from the copy", self._copy_company)
            vendors = [VendorDTO(**row) for row in copy[0]]
            return sorted(vendors, key=lambda vendor: (vendor.vendor_name or "", vendor.vendor_code))

    def _active_vendors_live(self) -> List[VendorDTO]:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            query = f"""
                SELECT
                    "CardCode"  AS vendor_code,
                    "CardName"  AS vendor_name
                FROM "{schema}"."OCRD"
                WHERE "CardType" = 'S'
                  AND "frozenFor" = 'N'
                ORDER BY "CardName"
            """

            cursor.execute(query)
            rows = cursor.fetchall()

            return [
                VendorDTO(
                    vendor_code=row[0],
                    vendor_name=row[1],
                )
                for row in rows
            ]

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error for vendors: {e}")
            raise SAPDataError(
                "Failed to retrieve vendor data from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for vendors: {e}")
            raise SAPDataError(
                "Failed to retrieve vendor data from SAP. Please try again later."
            ) from e
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
