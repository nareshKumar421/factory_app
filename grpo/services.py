import logging
import os
import re
import tempfile
from functools import lru_cache
from typing import List, Dict, Any, Optional
from decimal import Decimal
from datetime import date
from django.core.files import File
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum

from gate_core.enums import GateEntryStatus
from dispatch_plans.hana_reader import HanaDispatchBillReader
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import VehicleEntry
from raw_material_gatein.models import POReceipt, POItemReceipt
from quality_control.enums import InspectionStatus
from sap_client.client import SAPClient
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.connection import HanaConnection
from weighment.models import Weighment

from .models import (
    GRPOPosting,
    GRPOLinePosting,
    GRPOStatus,
    GRPOAttachment,
    SAPAttachmentStatus,
    ServiceGRPOPosting,
    ServiceGRPOLinePosting,
    ServiceGRPOAttachment,
)

logger = logging.getLogger(__name__)


class GRPOService:
    """
    Service for handling GRPO operations.
    """
    SAP_DOCUMENT_COMMENTS_MAX_LENGTH = 254
    QC_FINAL_STATUSES = frozenset({
        InspectionStatus.ACCEPTED,
        InspectionStatus.REJECTED,
    })
    STATE_NAME_CODES = {
        "HARYANA": "HR",
        "DELHI": "DL",
        "NEW DELHI": "DL",
        "PUNJAB": "PB",
        "UTTAR PRADESH": "UP",
        "RAJASTHAN": "RJ",
        "HIMACHAL PRADESH": "HP",
        "UTTARAKHAND": "UK",
        "CHANDIGARH": "CH",
    }

    def __init__(self, company_code: str):
        self.company_code = company_code

    @staticmethod
    @lru_cache(maxsize=32)
    def _get_sap_table_columns(
        company_code: str,
        table_name: str,
    ) -> Optional[frozenset[str]]:
        context = CompanyContext(company_code)
        connection = HanaConnection(context.hana)
        conn = None
        cursor = None
        try:
            conn = connection.connect()
            cursor = conn.cursor()
            cursor.execute(
                """
                    SELECT "COLUMN_NAME"
                    FROM "SYS"."TABLE_COLUMNS"
                    WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = ?
                """,
                [connection.schema, table_name.upper()],
            )
            return frozenset(row[0] for row in cursor.fetchall())
        except Exception as exc:
            logger.warning(
                "Could not read SAP column metadata for %s.%s: %s",
                company_code,
                table_name,
                exc,
            )
            return None
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

    def _filter_purchase_delivery_note_udfs(self, payload: Dict[str, Any]) -> None:
        """Drop company-missing UDFs before Service Layer rejects the GRPO."""
        header_columns = self._get_sap_table_columns(self.company_code, "OPDN")
        line_columns = self._get_sap_table_columns(self.company_code, "PDN1")

        if header_columns:
            for key in list(payload):
                if key.startswith("U_") and key not in header_columns:
                    logger.info(
                        "Skipping unsupported OPDN UDF %s for %s",
                        key,
                        self.company_code,
                    )
                    payload.pop(key, None)

        if line_columns:
            for line in payload.get("DocumentLines", []):
                for key in list(line):
                    if key.startswith("U_") and key not in line_columns:
                        logger.info(
                            "Skipping unsupported PDN1 UDF %s for %s",
                            key,
                            self.company_code,
                        )
                        line.pop(key, None)

    @classmethod
    def _truncate_sap_document_comments(cls, comments: str) -> str:
        """Keep SAP document comments within the Service Layer field limit."""
        comments = (comments or "").strip()
        if len(comments) <= cls.SAP_DOCUMENT_COMMENTS_MAX_LENGTH:
            return comments
        suffix = "..."
        max_body_length = cls.SAP_DOCUMENT_COMMENTS_MAX_LENGTH - len(suffix)
        return comments[:max_body_length].rstrip(" |,") + suffix

    @staticmethod
    def _decimal_or_none(value, decimal_places: str = "0.001") -> Optional[Decimal]:
        if value in (None, ""):
            return None
        decimal_value = Decimal(str(value))
        return decimal_value.quantize(Decimal(decimal_places))

    @staticmethod
    def _sap_absolute_entry_or_none(value) -> Optional[int]:
        if isinstance(value, bool) or value in (None, ""):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.strip().isdigit():
            entry = int(value.strip())
            return entry if entry > 0 else None
        return None

    @staticmethod
    def _first_day_of_month(value) -> Optional[date]:
        if not value:
            return None
        if isinstance(value, str):
            if re.fullmatch(r"\d{4}-\d{2}", value):
                value = f"{value}-01"
            try:
                value = date.fromisoformat(value)
            except ValueError:
                return None
        if not hasattr(value, "year") or not hasattr(value, "month"):
            return None
        return date(value.year, value.month, 1)

    @staticmethod
    def _infer_product_variety(item_summary: str) -> str:
        summary = (item_summary or "").lower()
        if any(token in summary for token in ("water", "mineral", "drink", "beverage", "juice")):
            return "Beverage"
        if summary:
            return "Oil"
        return ""

    @staticmethod
    def _infer_service_description(item_summary: str, product_variety: str = "") -> str:
        summary = (item_summary or "").lower()
        service_tokens = [
            (("water", "mineral"), "Water"),
            (("drink",), "Drink"),
            (("juice",), "Juice"),
            (("beverage",), "Beverage"),
            (("oil",), "Oil"),
        ]
        for tokens, label in service_tokens:
            if any(token in summary for token in tokens):
                return label
        return (product_variety or "").strip() or "Transport"

    @staticmethod
    @lru_cache(maxsize=32)
    def _get_sap_tax_codes(company_code: str) -> Dict[str, Dict[str, Any]]:
        context = CompanyContext(company_code)
        connection = HanaConnection(context.hana)
        conn = None
        cursor = None
        try:
            conn = connection.connect()
            cursor = conn.cursor()
            cursor.execute(
                f"""
                    SELECT "Code", IFNULL("Name", ''), "Rate"
                    FROM "{connection.schema}"."OSTC"
                """
            )
            codes = {}
            for code, name, rate in cursor.fetchall():
                if not code:
                    continue
                rate_value = Decimal(str(rate)) if rate is not None else None
                codes[str(code).strip().upper()] = {
                    "code": str(code).strip(),
                    "name": str(name or "").strip(),
                    "rate": rate_value,
                }
            return codes
        except Exception as exc:
            logger.warning("Could not read SAP tax codes for %s: %s", company_code, exc)
            return {}
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

    @staticmethod
    @lru_cache(maxsize=32)
    def _get_sap_branch_states(company_code: str) -> Dict[int, str]:
        context = CompanyContext(company_code)
        connection = HanaConnection(context.hana)
        conn = None
        cursor = None
        try:
            conn = connection.connect()
            cursor = conn.cursor()
            cursor.execute(
                f"""
                    SELECT "BPLId", IFNULL("State", '')
                    FROM "{connection.schema}"."OBPL"
                    WHERE IFNULL("Disabled", 'N') = 'N'
                """
            )
            return {
                int(row[0]): GRPOService._normalize_state(row[1])
                for row in cursor.fetchall()
                if row[0] is not None
            }
        except Exception as exc:
            logger.warning("Could not read SAP branch states for %s: %s", company_code, exc)
            return {}
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

    @staticmethod
    @lru_cache(maxsize=256)
    def _get_sap_bp_state(company_code: str, bp_code: str) -> str:
        bp_code = (bp_code or "").strip()
        if not bp_code:
            return ""

        context = CompanyContext(company_code)
        connection = HanaConnection(context.hana)
        conn = None
        cursor = None
        try:
            conn = connection.connect()
            cursor = conn.cursor()
            cursor.execute(
                f"""
                    SELECT IFNULL("State1", ''), IFNULL("State2", '')
                    FROM "{connection.schema}"."OCRD"
                    WHERE "CardCode" = ?
                """,
                [bp_code],
            )
            row = cursor.fetchone()
            if row:
                for state in row:
                    normalized_state = GRPOService._normalize_state(state)
                    if normalized_state:
                        return normalized_state

            cursor.execute(
                f"""
                    SELECT IFNULL("State", '')
                    FROM "{connection.schema}"."CRD1"
                    WHERE "CardCode" = ?
                      AND IFNULL("State", '') <> ''
                    ORDER BY
                        CASE
                            WHEN IFNULL("AdresType", '') = 'B' THEN 0
                            WHEN IFNULL("AdresType", '') = 'S' THEN 1
                            ELSE 2
                        END,
                        "Address"
                """,
                [bp_code],
            )
            for row in cursor.fetchall():
                normalized_state = GRPOService._normalize_state(row[0])
                if normalized_state:
                    return normalized_state
            return ""
        except Exception as exc:
            logger.warning(
                "Could not read SAP BP state for %s/%s: %s",
                company_code,
                bp_code,
                exc,
            )
            return ""
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

    @classmethod
    def _normalize_state(cls, value: Any) -> str:
        state = str(value or "").strip().upper()
        if not state:
            return ""
        match = re.search(r"\(([A-Z]{2})\)", state)
        if match:
            return match.group(1)
        if len(state) == 2:
            return state
        return cls.STATE_NAME_CODES.get(state, state)

    @staticmethod
    def _format_tax_rate(rate: Decimal) -> str:
        normalized = Decimal(str(rate)).normalize()
        if normalized == normalized.to_integral():
            return str(int(normalized))
        return format(normalized, "f").rstrip("0").rstrip(".")

    @classmethod
    def _tax_rate_for_code(
        cls,
        tax_code: str,
        tax_codes: Dict[str, Dict[str, Any]],
    ) -> Optional[Decimal]:
        code = (tax_code or "").strip()
        if not code:
            return None

        tax_record = tax_codes.get(code.upper())
        if tax_record and tax_record.get("rate") is not None:
            return Decimal(str(tax_record["rate"]))

        match = re.search(r"@(\d+(?:\.\d+)?)", code)
        if not match:
            match = re.search(r"(\d+(?:\.\d+)?)", code)
        if not match:
            return None
        return Decimal(match.group(1))

    @staticmethod
    def _tax_code_details(
        tax_code: str,
        tax_codes: Dict[str, Dict[str, Any]],
    ) -> str:
        code = (tax_code or "").strip()
        tax_record = tax_codes.get(code.upper())
        name = tax_record.get("name", "") if tax_record else ""
        return f"{code} {name}".upper()

    @classmethod
    def _is_rcm_tax_code(
        cls,
        tax_code: str,
        tax_codes: Dict[str, Dict[str, Any]],
    ) -> bool:
        details = cls._tax_code_details(tax_code, tax_codes)
        code = (tax_code or "").strip().upper()
        return (
            "RCM" in details
            or code.startswith(("RIGST", "RISGT", "RCGSG"))
            or code == "GST05R"
        )

    @classmethod
    def _is_igst_tax_code(
        cls,
        tax_code: str,
        tax_codes: Dict[str, Dict[str, Any]],
    ) -> bool:
        details = cls._tax_code_details(tax_code, tax_codes)
        return "IGST" in details or (tax_code or "").strip().upper().startswith(
            ("RIGST", "RISGT")
        )

    @staticmethod
    def _first_available_tax_code(
        tax_codes: Dict[str, Dict[str, Any]],
        candidates: List[str],
    ) -> str:
        if not tax_codes:
            return candidates[0] if candidates else ""
        for candidate in candidates:
            tax_record = tax_codes.get(candidate.upper())
            if tax_record:
                return tax_record["code"]
        return ""

    def _resolve_service_line_tax_code(
        self,
        requested_tax_code: Optional[str],
        branch_state: str,
        supply_state: str,
    ) -> str:
        requested_tax_code = (requested_tax_code or "").strip()
        if not requested_tax_code:
            return ""

        branch_state = self._normalize_state(branch_state)
        supply_state = self._normalize_state(supply_state)
        if not branch_state or not supply_state:
            return requested_tax_code

        tax_codes = self._get_sap_tax_codes(self.company_code)
        tax_rate = self._tax_rate_for_code(requested_tax_code, tax_codes)
        if tax_rate is None:
            return requested_tax_code

        is_interstate = branch_state != supply_state
        is_rcm = self._is_rcm_tax_code(requested_tax_code, tax_codes)
        is_igst = self._is_igst_tax_code(requested_tax_code, tax_codes)
        rate_key = self._format_tax_rate(tax_rate)

        if is_interstate:
            if is_igst:
                return requested_tax_code
            candidates = (
                [f"RIGST@{rate_key}", f"RISGT@{rate_key}", f"IGST@{rate_key}"]
                if is_rcm
                else [f"IGST@{rate_key}", f"RIGST@{rate_key}"]
            )
            return self._first_available_tax_code(tax_codes, candidates) or requested_tax_code

        if not is_igst:
            return requested_tax_code

        candidates = (
            ["GST05R", f"RCGSG@{rate_key}", f"CG+SG@{rate_key}"]
            if is_rcm and rate_key == "5"
            else [f"RCGSG@{rate_key}", f"CG+SG@{rate_key}"]
            if is_rcm
            else [f"CG+SG@{rate_key}"]
        )
        return self._first_available_tax_code(tax_codes, candidates) or requested_tax_code

    @staticmethod
    @lru_cache(maxsize=32)
    def _get_active_dimension_codes(
        company_code: str,
        dim_code: int,
    ) -> Optional[Dict[str, str]]:
        context = CompanyContext(company_code)
        connection = HanaConnection(context.hana)
        conn = None
        cursor = None
        try:
            conn = connection.connect()
            cursor = conn.cursor()
            cursor.execute(
                f"""
                    SELECT "OcrCode", IFNULL("OcrName", '')
                    FROM "{connection.schema}"."OOCR"
                    WHERE "DimCode" = ?
                      AND IFNULL("Active", 'Y') = 'Y'
                """,
                [dim_code],
            )
            return {
                str(row[0]).strip(): str(row[1] or row[0]).strip()
                for row in cursor.fetchall()
                if row[0]
            }
        except Exception as exc:
            logger.warning(
                "Could not read active SAP dimension %s distribution rules for %s: %s",
                dim_code,
                company_code,
                exc,
            )
            return None
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

    @classmethod
    def _get_active_budget_codes(cls, company_code: str) -> Optional[Dict[str, str]]:
        return cls._get_active_dimension_codes(company_code, 3)

    def _is_active_budget_code(self, budget_code: str) -> bool:
        budget_code = (budget_code or "").strip()
        if not budget_code:
            return False
        active_budget_codes = self._get_active_budget_codes(self.company_code)
        if active_budget_codes is None:
            return True
        return budget_code in active_budget_codes

    @staticmethod
    def _format_effective_month_dimension(value: Optional[date]) -> str:
        if not value:
            return ""
        return value.strftime("%m-%Y")

    def _resolve_active_dimension_code(
        self,
        dim_code: int,
        *candidates: Any,
    ) -> str:
        active_codes = self._get_active_dimension_codes(self.company_code, dim_code)
        cleaned_candidates = [
            str(candidate or "").strip()
            for candidate in candidates
            if str(candidate or "").strip()
        ]
        if not cleaned_candidates:
            return ""
        if active_codes is None:
            return cleaned_candidates[0]

        lower_lookup = {
            code.lower(): code
            for code in active_codes
        }
        name_lookup = {
            name.lower(): code
            for code, name in active_codes.items()
            if name
        }
        for candidate in cleaned_candidates:
            variants = [candidate, candidate.upper()]
            for variant in variants:
                if variant in active_codes:
                    return variant
                matched_code = lower_lookup.get(variant.lower()) or name_lookup.get(
                    variant.lower()
                )
                if matched_code:
                    return matched_code
        return ""

    @staticmethod
    def _normalize_dimension_search_text(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    def _product_dimension_candidates_from_summary(
        self,
        item_summary: str,
    ) -> List[str]:
        normalized_summary = self._normalize_dimension_search_text(item_summary)
        if not normalized_summary:
            return []

        candidates = []
        active_codes = self._get_active_dimension_codes(self.company_code, 1) or {}

        # Prefer exact SAP master-data matches from invoice text. This catches
        # cases like "COLD PRESS SUNFLOWER" -> OcrCode "SUNFLOWR".
        for code, name in active_codes.items():
            for value in (name, code):
                token = self._normalize_dimension_search_text(value)
                if len(token) >= 4 and token in normalized_summary:
                    candidates.append(code)
                    break

        synonym_candidates = [
            (("sunflower",), ["SUNFLOWER", "SUNFLOWR"]),
            (("groundnut", "peanut"), ["GROUNDNUT", "GROUNDNT"]),
            (("mustard",), ["MUSTARD"]),
            (("canola",), ["CANOLA"]),
            (("coconut",), ["COCONUT"]),
            (("olive",), ["OLIVE"]),
            (("palm",), ["PALM OIL"]),
            (("rice bran", "ricebran"), ["RICE BRAN", "RICEBRAN"]),
            (("soyabean", "soybean", "soya"), ["SOYABEAN"]),
            (("sesame",), ["SESAME"]),
            (("cotton seed", "cottonseed"), ["COTTON SEED", "COTTONSD"]),
            (("ghee",), ["GHEE"]),
            (("water", "mineral"), ["WATER", "WATRCMPR"]),
            (("juice",), ["JUICE"]),
            (("drink", "beverage"), ["BEVERAGE"]),
        ]
        for tokens, values in synonym_candidates:
            if any(token in normalized_summary for token in tokens):
                candidates.extend(values)

        return candidates

    def _product_dimension_candidates_from_value(self, value: Any) -> List[str]:
        normalized_value = self._normalize_dimension_search_text(value)
        if not normalized_value:
            return []

        candidates = [str(value).strip()]
        active_codes = self._get_active_dimension_codes(self.company_code, 1) or {}

        for code, name in active_codes.items():
            searchable = self._normalize_dimension_search_text(f"{code} {name}")
            if len(normalized_value) >= 3 and normalized_value in searchable:
                candidates.append(code)

        synonym_tokens = {
            "oil": ("oil", "crude", "edible"),
            "transport": ("oil", "transport", "freight"),
            "freight": ("oil", "transport", "freight"),
            "beverage": ("beverage", "water", "juice", "drink"),
            "water": ("water", "beverage"),
        }
        for key, tokens in synonym_tokens.items():
            if key not in normalized_value:
                continue
            for code, name in active_codes.items():
                searchable = self._normalize_dimension_search_text(f"{code} {name}")
                if any(token in searchable for token in tokens):
                    candidates.append(code)

        return list(dict.fromkeys(candidates))

    def _resolve_product_dimension_code(
        self,
        item_summary: str,
        product_variety: str,
        service_description: str,
    ) -> str:
        candidates = [
            *self._product_dimension_candidates_from_summary(item_summary),
            *self._product_dimension_candidates_from_value(product_variety),
            *self._product_dimension_candidates_from_value(service_description),
        ]
        return self._resolve_active_dimension_code(1, *candidates)

    def _infer_budget_delivery_point(self, dispatch_plan: DispatchPlan) -> str:
        active_budget_codes = self._get_active_budget_codes(self.company_code) or {}
        saved_budget = (dispatch_plan.budget_delivery_point or "").strip()
        if saved_budget in active_budget_codes:
            return saved_budget

        normalized_saved_budget = saved_budget.lower()
        if normalized_saved_budget:
            for code, name in active_budget_codes.items():
                if normalized_saved_budget in {code.lower(), name.lower()}:
                    return code

        location_hint = (
            f"{dispatch_plan.service_location_name} {dispatch_plan.service_location_code or ''}"
        ).lower()
        if any(token in location_hint for token in ("mayapuri", "delhi isd")):
            if "Del Mayp" in active_budget_codes:
                return "Del Mayp"

        if "Del Bkhp" in active_budget_codes:
            return "Del Bkhp"
        return next(iter(active_budget_codes), saved_budget)

    @staticmethod
    def _infer_service_sub_account(bill_snapshot: Dict[str, Any]) -> str:
        return "SALES" if bill_snapshot.get("card_code") else ""

    @staticmethod
    def _dispatch_linked_vehicle_entry_id(dispatch_plan: DispatchPlan) -> Optional[int]:
        return dispatch_plan.linked_vehicle_entry_id

    @staticmethod
    def _dispatch_linked_vehicle_entry_no(dispatch_plan: DispatchPlan) -> str:
        if dispatch_plan.linked_vehicle_entry_id:
            return dispatch_plan.linked_vehicle_entry.entry_no
        return ""

    @staticmethod
    def _dispatch_vehicle_no(dispatch_plan: DispatchPlan) -> str:
        if dispatch_plan.vehicle_no:
            return dispatch_plan.vehicle_no
        if dispatch_plan.linked_vehicle_entry_id and dispatch_plan.linked_vehicle_entry.vehicle_id:
            return dispatch_plan.linked_vehicle_entry.vehicle.vehicle_number
        if dispatch_plan.vehicle_id:
            return dispatch_plan.vehicle.vehicle_number
        return ""

    @staticmethod
    def _dispatch_driver_name(dispatch_plan: DispatchPlan) -> str:
        if dispatch_plan.driver_name:
            return dispatch_plan.driver_name
        if dispatch_plan.linked_vehicle_entry_id and dispatch_plan.linked_vehicle_entry.driver_id:
            return dispatch_plan.linked_vehicle_entry.driver.name
        if dispatch_plan.driver_id:
            return dispatch_plan.driver.name
        return ""

    @staticmethod
    def _dispatch_transporter(dispatch_plan: DispatchPlan):
        if dispatch_plan.transporter_id:
            return dispatch_plan.transporter
        if (
            dispatch_plan.linked_vehicle_entry_id
            and dispatch_plan.linked_vehicle_entry.vehicle_id
            and dispatch_plan.linked_vehicle_entry.vehicle.transporter_id
        ):
            return dispatch_plan.linked_vehicle_entry.vehicle.transporter
        if dispatch_plan.vehicle_id and dispatch_plan.vehicle.transporter_id:
            return dispatch_plan.vehicle.transporter
        return None

    def _dispatch_transporter_name(self, dispatch_plan: DispatchPlan) -> str:
        if dispatch_plan.transporter_name:
            return dispatch_plan.transporter_name
        transporter = self._dispatch_transporter(dispatch_plan)
        if transporter:
            return transporter.name
        return ""

    def _dispatch_transporter_gstin(self, dispatch_plan: DispatchPlan) -> str:
        if dispatch_plan.transporter_gstin:
            return dispatch_plan.transporter_gstin
        transporter = self._dispatch_transporter(dispatch_plan)
        if transporter:
            return getattr(transporter, "gstin", "") or ""
        return ""

    @staticmethod
    def _service_group_key(dispatch_plan: DispatchPlan) -> tuple:
        bilty_no = (dispatch_plan.bilty_no or "").strip()
        if not bilty_no:
            return ("dispatch-plan", dispatch_plan.id)
        return (
            "bilty",
            bilty_no,
            dispatch_plan.bilty_date,
            dispatch_plan.linked_vehicle_entry_id or 0,
            dispatch_plan.vehicle_id or 0,
            dispatch_plan.transporter_id or 0,
            (dispatch_plan.vehicle_no or "").strip().upper(),
        )

    def _get_service_group_plans(self, dispatch_plan: DispatchPlan) -> List[DispatchPlan]:
        bilty_no = (dispatch_plan.bilty_no or "").strip()
        if not bilty_no:
            return [dispatch_plan]

        queryset = DispatchPlan.objects.filter(
            company=dispatch_plan.company,
            booking_status=DispatchPlanStatus.BOOKED,
            is_active=True,
            bilty_no=bilty_no,
            bilty_date=dispatch_plan.bilty_date,
        )
        if dispatch_plan.linked_vehicle_entry_id:
            queryset = queryset.filter(
                linked_vehicle_entry_id=dispatch_plan.linked_vehicle_entry_id
            )
        elif dispatch_plan.vehicle_id:
            queryset = queryset.filter(vehicle_id=dispatch_plan.vehicle_id)
        elif dispatch_plan.vehicle_no:
            queryset = queryset.filter(vehicle_no__iexact=dispatch_plan.vehicle_no)

        if dispatch_plan.transporter_id:
            queryset = queryset.filter(transporter_id=dispatch_plan.transporter_id)
        elif dispatch_plan.transporter_name:
            queryset = queryset.filter(transporter_name__iexact=dispatch_plan.transporter_name)

        return list(
            queryset.select_related(
                "company",
                "vehicle",
                "vehicle__transporter",
                "transporter",
                "driver",
                "linked_vehicle_entry",
                "linked_vehicle_entry__vehicle",
                "linked_vehicle_entry__vehicle__transporter",
                "linked_vehicle_entry__driver",
            ).order_by("sap_invoice_doc_num", "sap_invoice_doc_entry", "id")
        )

    @staticmethod
    def get_service_display_vehicle_no(dispatch_plan: DispatchPlan) -> str:
        if dispatch_plan.vehicle_no:
            return dispatch_plan.vehicle_no
        if dispatch_plan.vehicle_id and getattr(dispatch_plan, "vehicle", None):
            return dispatch_plan.vehicle.vehicle_number or ""
        linked_entry = getattr(dispatch_plan, "linked_vehicle_entry", None)
        if linked_entry and getattr(linked_entry, "vehicle", None):
            return linked_entry.vehicle.vehicle_number or ""
        return ""

    @staticmethod
    def get_service_display_driver_name(dispatch_plan: DispatchPlan) -> str:
        if dispatch_plan.driver_name:
            return dispatch_plan.driver_name
        if dispatch_plan.driver_id and getattr(dispatch_plan, "driver", None):
            return dispatch_plan.driver.name or ""
        linked_entry = getattr(dispatch_plan, "linked_vehicle_entry", None)
        if linked_entry and getattr(linked_entry, "driver", None):
            return linked_entry.driver.name or ""
        return ""

    @staticmethod
    def get_service_display_linked_entry_no(dispatch_plan: DispatchPlan) -> str:
        linked_entry = getattr(dispatch_plan, "linked_vehicle_entry", None)
        return linked_entry.entry_no if linked_entry else ""

    @staticmethod
    def _line_amount_from_plan(dispatch_plan: DispatchPlan) -> Decimal:
        amount = dispatch_plan.total_freight
        if amount is None:
            amount = dispatch_plan.freight
        if amount is None:
            return Decimal("0.00")
        return Decimal(str(amount)).quantize(Decimal("0.01"))

    @staticmethod
    def _allocate_amount_by_weight(
        total_amount: Decimal,
        weights: List[Decimal],
    ) -> List[Decimal]:
        if not weights:
            return []
        cleaned_weights = [weight if weight > 0 else Decimal("1") for weight in weights]
        total_weight = sum(cleaned_weights, Decimal("0"))
        allocations = []
        running_total = Decimal("0.00")
        for index, weight in enumerate(cleaned_weights):
            if index == len(cleaned_weights) - 1:
                allocation = total_amount - running_total
            else:
                allocation = (total_amount * weight / total_weight).quantize(
                    Decimal("0.01"),
                    rounding="ROUND_HALF_UP",
                )
                running_total += allocation
            allocations.append(allocation)
        return allocations

    def _posted_service_grpo_for_group(
        self,
        group_plans: List[DispatchPlan],
    ) -> Optional[ServiceGRPOPosting]:
        plan_ids = [plan.id for plan in group_plans]
        return (
            ServiceGRPOPosting.objects.filter(
                status=GRPOStatus.POSTED,
            )
            .filter(
                Q(dispatch_plan_id__in=plan_ids)
                | Q(lines__dispatch_plan_id__in=plan_ids)
            )
            .distinct()
            .order_by("-created_at")
            .first()
        )

    def _latest_service_grpo_for_group(
        self,
        group_plans: List[DispatchPlan],
    ) -> Optional[ServiceGRPOPosting]:
        plan_ids = [plan.id for plan in group_plans]
        return (
            ServiceGRPOPosting.objects.filter(
                Q(dispatch_plan_id__in=plan_ids)
                | Q(lines__dispatch_plan_id__in=plan_ids)
            )
            .distinct()
            .order_by("-created_at")
            .first()
        )

    def _get_dispatch_bill_snapshot(self, dispatch_plan: DispatchPlan) -> Dict[str, Any]:
        doc_num = (dispatch_plan.sap_invoice_doc_num or "").strip()
        if not doc_num:
            return {}
        try:
            reader = HanaDispatchBillReader(CompanyContext(self.company_code))
            return reader.get_bill_by_number(doc_num) or {}
        except Exception as exc:
            logger.warning(
                "Could not fetch dispatch SAP bill snapshot for service GRPO plan %s: %s",
                dispatch_plan.id,
                exc,
            )
            return {}

    def get_pending_grpo_entries(self) -> List[VehicleEntry]:
        """
        Get all completed gate entries that are ready for GRPO posting.
        Returns entries with status COMPLETED or QC_COMPLETED, excluding stale
        entries whose item QC is still HOLD/PENDING.
        """
        entries = list(VehicleEntry.objects.filter(
            company__code=self.company_code,
            entry_type="RAW_MATERIAL",
            is_active=True,
            status__in=[GateEntryStatus.COMPLETED, GateEntryStatus.QC_COMPLETED]
        ).prefetch_related(
            "po_receipts",
            "po_receipts__items",
            "po_receipts__items__arrival_slip",
            "po_receipts__items__arrival_slip__inspection",
            "grpo_postings"
        ).order_by("-entry_time"))
        return [entry for entry in entries if self.is_entry_ready_for_grpo(entry)]

    def get_grpo_dashboard_summary(self) -> Dict[str, Any]:
        """
        Build the material GRPO dashboard summary from the correct sources.

        Pending metrics come from GRPO-ready gate entries and unposted POs.
        QC accepted/rejected metrics come from PO item receipt quantities.
        Posting metrics come from GRPOPosting history statuses.
        """
        pending_entry_count = 0
        pending_po_count = 0

        for entry in self.get_pending_grpo_entries():
            po_receipts = list(entry.po_receipts.all())
            posted_po_ids = set()

            for grpo in entry.grpo_postings.filter(status=GRPOStatus.POSTED):
                posted_po_ids.update(grpo.po_receipts.values_list("id", flat=True))
                if grpo.po_receipt_id:
                    posted_po_ids.add(grpo.po_receipt_id)

            entry_pending_po_count = sum(
                1 for po_receipt in po_receipts if po_receipt.id not in posted_po_ids
            )
            if entry_pending_po_count:
                pending_entry_count += 1
                pending_po_count += entry_pending_po_count

        item_totals = POItemReceipt.objects.filter(
            is_active=True,
            po_receipt__is_active=True,
            po_receipt__vehicle_entry__company__code=self.company_code,
            po_receipt__vehicle_entry__entry_type="RAW_MATERIAL",
            po_receipt__vehicle_entry__is_active=True,
        ).exclude(
            po_receipt__vehicle_entry__status=GateEntryStatus.CANCELLED,
        ).aggregate(
            accepted_qty=Sum("accepted_qty"),
            rejected_qty=Sum("rejected_qty"),
        )

        posting_counts = {
            status_key: GRPOPosting.objects.filter(
                vehicle_entry__company__code=self.company_code,
                vehicle_entry__is_active=True,
                status=status_key,
            ).count()
            for status_key in GRPOStatus.values
        }

        return {
            "pending_entry_count": pending_entry_count,
            "pending_po_count": pending_po_count,
            "qc_accepted_qty": item_totals["accepted_qty"] or Decimal("0"),
            "qc_rejected_qty": item_totals["rejected_qty"] or Decimal("0"),
            "posting_pending_count": posting_counts[GRPOStatus.PENDING],
            "posted_count": posting_counts[GRPOStatus.POSTED],
            "failed_count": posting_counts[GRPOStatus.FAILED],
            "partially_posted_count": posting_counts[GRPOStatus.PARTIALLY_POSTED],
        }

    def get_all_grpo_visible_entries(self) -> List[VehicleEntry]:
        """
        Get all RAW_MATERIAL gate entries the GRPO operator may want to see —
        including in-flight ones still at gate or QC. Cancelled entries are
        excluded; the GRPO operator has no action on them.
        """
        return VehicleEntry.objects.filter(
            company__code=self.company_code,
            entry_type="RAW_MATERIAL",
            is_active=True,
        ).exclude(
            status=GateEntryStatus.CANCELLED,
        ).prefetch_related(
            "po_receipts",
            "po_receipts__items",
            "po_receipts__items__arrival_slip",
            "po_receipts__items__arrival_slip__inspection",
            "grpo_postings",
        ).order_by("-entry_time")

    def get_grpo_preview_data(
        self,
        vehicle_entry_id: int,
        po_receipt_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all data required for GRPO posting for a specific gate entry.
        Optionally filter by specific PO receipt IDs (for merged preview).
        Returns list of PO receipts with their items and QC status.
        """
        try:
            vehicle_entry = VehicleEntry.objects.prefetch_related(
                "po_receipts",
                "po_receipts__items",
                "po_receipts__items__arrival_slip",
                "po_receipts__items__arrival_slip__inspection",
                "grpo_postings"
            ).get(
                id=vehicle_entry_id,
                company__code=self.company_code,
                is_active=True,
            )
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")

        is_ready = self.is_entry_ready_for_grpo(vehicle_entry)

        po_receipts_qs = vehicle_entry.po_receipts.all()
        if po_receipt_ids:
            po_receipts_qs = po_receipts_qs.filter(id__in=po_receipt_ids)

        result = []
        for po_receipt in po_receipts_qs:
            # Check if GRPO already posted for this PO (M2M or legacy FK)
            existing_grpo = GRPOPosting.objects.filter(
                po_receipts=po_receipt,
                status=GRPOStatus.POSTED
            ).first()
            if not existing_grpo:
                existing_grpo = vehicle_entry.grpo_postings.filter(
                    po_receipt=po_receipt,
                    status=GRPOStatus.POSTED
                ).first()

            items_data = []
            for item in po_receipt.items.all():
                qc_status = self._get_item_qc_status(item)
                items_data.append({
                    "po_item_receipt_id": item.id,
                    "item_code": item.po_item_code,
                    "item_name": item.item_name,
                    "ordered_qty": item.ordered_qty,
                    "received_qty": item.received_qty,
                    "accepted_qty": item.accepted_qty,
                    "rejected_qty": item.rejected_qty,
                    "uom": item.uom,
                    "qc_status": qc_status,
                    "unit_price": item.unit_price,
                    "tax_code": item.tax_code or "",
                    "warehouse_code": item.warehouse_code or "",
                    "gl_account": item.gl_account or "",
                    "variety": item.variety or "",
                    "sap_line_num": item.sap_line_num,
                })

            result.append({
                "vehicle_entry_id": vehicle_entry.id,
                "entry_no": vehicle_entry.entry_no,
                "entry_status": vehicle_entry.status,
                "entry_date": vehicle_entry.entry_time.date() if vehicle_entry.entry_time else None,
                "is_ready_for_grpo": is_ready,
                "po_receipt_id": po_receipt.id,
                "po_number": po_receipt.po_number,
                "supplier_code": po_receipt.supplier_code,
                "supplier_name": po_receipt.supplier_name,
                "sap_doc_entry": po_receipt.sap_doc_entry,
                "branch_id": po_receipt.branch_id,
                "vendor_ref": po_receipt.vendor_ref or "",
                "invoice_no": po_receipt.invoice_no or "",
                "invoice_date": po_receipt.invoice_date,
                "challan_no": po_receipt.challan_no or "",
                "items": items_data,
                "grpo_status": existing_grpo.status if existing_grpo else None,
                "sap_doc_num": existing_grpo.sap_doc_num if existing_grpo else None,
                "total_amount": existing_grpo.sap_doc_total if existing_grpo else None
            })

        return result

    def _get_item_qc_status(self, po_item_receipt: POItemReceipt) -> str:
        """Get QC status for a PO item receipt."""
        if not hasattr(po_item_receipt, "arrival_slip"):
            return "NO_ARRIVAL_SLIP"

        arrival_slip = po_item_receipt.arrival_slip
        if not arrival_slip.is_submitted:
            return "ARRIVAL_SLIP_PENDING"

        if not hasattr(arrival_slip, "inspection"):
            return "INSPECTION_PENDING"

        inspection = arrival_slip.inspection
        return inspection.final_status

    def _get_qc_blocking_reason(self, po_item_receipt: POItemReceipt) -> Optional[str]:
        """
        Return a QC status that blocks GRPO, if the item is still in QC flow.

        Existing non-QC/legacy gate entries can still proceed when no arrival
        slip exists. Once QC has started, the item must reach ACCEPTED or
        REJECTED before GRPO can be listed or posted.
        """
        if not hasattr(po_item_receipt, "arrival_slip"):
            return None

        arrival_slip = po_item_receipt.arrival_slip
        if not arrival_slip.is_submitted:
            return "ARRIVAL_SLIP_PENDING"

        if not hasattr(arrival_slip, "inspection"):
            return "INSPECTION_PENDING"

        inspection = arrival_slip.inspection
        if inspection.final_status not in self.QC_FINAL_STATUSES:
            return inspection.final_status or InspectionStatus.PENDING

        return None

    def _collect_qc_blockers(self, po_receipts: List[POReceipt]) -> List[str]:
        blockers = []
        for po_receipt in po_receipts:
            for item in po_receipt.items.all():
                status_value = self._get_qc_blocking_reason(item)
                if not status_value:
                    continue

                report_no = ""
                arrival_slip = getattr(item, "arrival_slip", None)
                inspection = getattr(arrival_slip, "inspection", None) if arrival_slip else None
                if inspection:
                    report_no = f", report {inspection.report_no}"

                blockers.append(
                    f"PO {po_receipt.po_number}, item {item.po_item_code}"
                    f"{report_no}, QC status {status_value}"
                )

        return blockers

    def is_entry_ready_for_grpo(self, vehicle_entry: VehicleEntry) -> bool:
        """Status plus item-level QC guard for GRPO readiness."""
        if vehicle_entry.status not in [
            GateEntryStatus.COMPLETED,
            GateEntryStatus.QC_COMPLETED
        ]:
            return False

        po_receipts = list(vehicle_entry.po_receipts.all())
        return not self._collect_qc_blockers(po_receipts)

    def _validate_qc_ready_for_grpo(self, vehicle_entry: VehicleEntry) -> None:
        po_receipts = list(
            POReceipt.objects.prefetch_related(
                "items",
                "items__arrival_slip",
                "items__arrival_slip__inspection",
            ).filter(vehicle_entry=vehicle_entry, is_active=True)
        )
        blockers = self._collect_qc_blockers(po_receipts)
        if blockers:
            raise ValueError(
                "GRPO cannot be posted because QC is not final for one or more "
                "related PO items: "
                + "; ".join(blockers)
            )

    def _build_structured_comments(
        self,
        user,
        po_receipts: List[POReceipt],
        vehicle_entry: VehicleEntry,
        user_comments: Optional[str] = None
    ) -> str:
        """Build structured comments string for SAP GRPO."""
        full_name = user.get_full_name() if hasattr(user, 'get_full_name') else str(user)
        username = getattr(user, 'username', getattr(user, 'email', str(user)))

        po_numbers = ", ".join(po.po_number for po in po_receipts)
        parts = [
            f"App: FactoryApp v2",
            f"User: {full_name} ({username})",
            f"PO: {po_numbers}",
            f"Gate Entry: {vehicle_entry.entry_no}",
        ]

        if len(po_receipts) > 1:
            parts.append(f"Merged: {len(po_receipts)} POs")

        if user_comments:
            parts.append(user_comments)

        return self._truncate_sap_document_comments(" | ".join(parts))

    @transaction.atomic
    def post_grpo(
        self,
        vehicle_entry_id: int,
        po_receipt_ids: List[int],
        user,
        items: List[Dict[str, Any]],
        branch_id: int,
        warehouse_code: Optional[str] = None,
        comments: Optional[str] = None,
        vendor_ref: Optional[str] = None,
        tare_weight: Optional[Decimal] = None,
        extra_charges: Optional[List[Dict[str, Any]]] = None,
        attachments: Optional[list] = None,
        doc_date: Optional[str] = None,
        doc_due_date: Optional[str] = None,
        tax_date: Optional[str] = None,
        should_roundoff: bool = False,
    ) -> GRPOPosting:
        """
        Post GRPO to SAP for one or more PO receipts (merged GRPO).
        All PO receipts must belong to the same supplier and vehicle entry.

        Args:
            vehicle_entry_id: ID of the vehicle entry
            po_receipt_ids: List of PO receipt IDs to merge into single GRPO
            user: User posting the GRPO
            items: List of dicts with po_item_receipt_id, accepted_qty, and optional fields
            branch_id: SAP Branch/Business Place ID (BPLId)
            warehouse_code: Optional warehouse code for SAP
            comments: Optional user comments for SAP document
            vendor_ref: Optional vendor reference number (NumAtCard)
            tare_weight: Optional tare weight captured at GRPO; updates the gate weighment row
            extra_charges: Optional list of additional expense dicts
            attachments: Optional list of Django UploadedFile objects to attach
            doc_date: Optional posting date (DocDate), ISO format YYYY-MM-DD
            doc_due_date: Optional due date (DocDueDate), ISO format YYYY-MM-DD
            tax_date: Optional document date (TaxDate), ISO format YYYY-MM-DD
            should_roundoff: If True, auto-calculates RoundDif to round the subtotal to the nearest integer
        """
        # Get vehicle entry
        try:
            vehicle_entry = VehicleEntry.objects.get(
                id=vehicle_entry_id,
                company__code=self.company_code,
                is_active=True,
            )
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")

        # Get all PO receipts
        po_receipts = list(
            POReceipt.objects.prefetch_related(
                "items",
                "items__arrival_slip",
                "items__arrival_slip__inspection"
            ).filter(id__in=po_receipt_ids, vehicle_entry=vehicle_entry)
        )

        if len(po_receipts) != len(po_receipt_ids):
            found_ids = {po.id for po in po_receipts}
            missing_ids = set(po_receipt_ids) - found_ids
            raise ValueError(f"PO receipt(s) not found for this vehicle entry: {missing_ids}")

        # Validate all POs have the same supplier
        supplier_codes = set(po.supplier_code for po in po_receipts)
        if len(supplier_codes) > 1:
            raise ValueError(
                f"Cannot merge POs from different suppliers. "
                f"Found suppliers: {supplier_codes}"
            )

        # Validate all POs have the same branch_id
        branch_ids = set(po.branch_id for po in po_receipts if po.branch_id is not None)
        if len(branch_ids) > 1:
            raise ValueError(
                f"Cannot merge POs with different branch IDs. "
                f"Found branch IDs: {branch_ids}"
            )

        # Validate gate entry status
        if vehicle_entry.status not in [
            GateEntryStatus.COMPLETED,
            GateEntryStatus.QC_COMPLETED
        ]:
            raise ValueError(
                f"Gate entry is not completed. Current status: {vehicle_entry.status}"
            )

        self._validate_qc_ready_for_grpo(vehicle_entry)

        weighment = (
            Weighment.objects.select_for_update()
            .filter(vehicle_entry=vehicle_entry)
            .first()
        )
        if tare_weight is not None:
            if tare_weight <= 0:
                raise ValueError("Tare weight must be greater than zero")
            if (
                weighment
                and weighment.gross_weight is not None
                and weighment.gross_weight > 0
                and tare_weight > weighment.gross_weight
            ):
                raise ValueError("Tare weight cannot be greater than gross weight")

            if weighment is None:
                weighment = Weighment(vehicle_entry=vehicle_entry, created_by=user)

            weighment.tare_weight = tare_weight
            if weighment.second_weighment_time is None:
                weighment.second_weighment_time = timezone.now()
            weighment.updated_by = user
            if weighment.pk:
                weighment.save(update_fields=[
                    "tare_weight",
                    "net_weight",
                    "second_weighment_time",
                    "updated_by",
                    "updated_at",
                ])
            else:
                weighment.save()

        # Check if any PO already has a POSTED GRPO
        for po_receipt in po_receipts:
            existing = GRPOPosting.objects.filter(
                po_receipts=po_receipt,
                status=GRPOStatus.POSTED
            ).first()
            if not existing:
                # Also check legacy po_receipt FK
                existing = GRPOPosting.objects.filter(
                    vehicle_entry=vehicle_entry,
                    po_receipt=po_receipt,
                    status=GRPOStatus.POSTED
                ).first()
            if existing:
                raise ValueError(
                    f"GRPO already posted for PO {po_receipt.po_number}. "
                    f"SAP Doc Num: {existing.sap_doc_num}"
                )

        # Collect all item IDs across all PO receipts
        all_po_item_ids = set()
        for po_receipt in po_receipts:
            all_po_item_ids.update(po_receipt.items.values_list("id", flat=True))

        # Create a mapping of item IDs to input data
        items_input_map = {item["po_item_receipt_id"]: item for item in items}

        # Validate all item IDs belong to one of the selected PO receipts
        invalid_ids = set(items_input_map.keys()) - all_po_item_ids
        if invalid_ids:
            raise ValueError(f"Invalid PO item receipt IDs: {invalid_ids}")

        # Update accepted and rejected quantities in POItemReceipt
        for po_receipt in po_receipts:
            for item in po_receipt.items.all():
                if item.id in items_input_map:
                    accepted_qty = items_input_map[item.id]["accepted_qty"]
                    item.accepted_qty = accepted_qty
                    item.rejected_qty = max(item.received_qty - accepted_qty, Decimal("0"))
                    item.save()

        # Create GRPO posting record (use first PO as legacy po_receipt)
        grpo_posting = GRPOPosting.objects.create(
            vehicle_entry=vehicle_entry,
            po_receipt=po_receipts[0],
            status=GRPOStatus.PENDING,
            posted_by=user
        )
        # Link all PO receipts via M2M
        grpo_posting.po_receipts.set(po_receipts)

        # Build GRPO document lines from ALL PO receipts
        document_lines = []
        grpo_lines_data = []

        for po_receipt in po_receipts:
            for item in po_receipt.items.all():
                if item.accepted_qty <= 0:
                    continue

                item_input = items_input_map.get(item.id, {})

                line_data = {
                    "ItemCode": item.po_item_code,
                    "Quantity": str(item.accepted_qty),
                }

                # PO Linking — each line references its own PO's BaseEntry
                if po_receipt.sap_doc_entry and item.sap_line_num is not None:
                    line_data["BaseEntry"] = po_receipt.sap_doc_entry
                    line_data["BaseLine"] = item.sap_line_num
                    line_data["BaseType"] = 22  # Purchase Order

                if warehouse_code:
                    line_data["WarehouseCode"] = warehouse_code

                unit_price = item_input.get("unit_price")
                if unit_price is not None:
                    line_data["UnitPrice"] = float(unit_price)

                tax_code = item_input.get("tax_code")
                if tax_code:
                    line_data["TaxCode"] = tax_code

                gl_account = item_input.get("gl_account")
                if gl_account:
                    line_data["AccountCode"] = gl_account

                variety = (item_input.get("variety") or item.variety or "").strip()
                if variety:
                    line_data["U_Variety"] = variety
                    line_data["CostingCode"] = variety
                    line_data["U_Variety"] = str(variety)[:50]

                document_lines.append(line_data)
                grpo_lines_data.append({
                    "po_item_receipt": item,
                    "quantity_posted": item.accepted_qty,
                    "base_entry": po_receipt.sap_doc_entry,
                    "base_line": item.sap_line_num,
                })

        if not document_lines:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = "No accepted quantities to post"
            grpo_posting.save()
            raise ValueError("No accepted quantities to post")

        # Build structured comments
        structured_comments = self._build_structured_comments(
            user, po_receipts, vehicle_entry, comments
        )

        # Build full SAP payload — CardCode from any PO (all same supplier)
        grpo_payload = {
            "CardCode": po_receipts[0].supplier_code,
            "BPL_IDAssignedToInvoice": branch_id,
            "Comments": structured_comments,
            "DocumentLines": document_lines
        }

        # Optional date fields
        if doc_date:
            grpo_payload["DocDate"] = str(doc_date)
        if doc_due_date:
            grpo_payload["DocDueDate"] = str(doc_due_date)
        if tax_date:
            grpo_payload["TaxDate"] = str(tax_date)

        # Auto round-off
        if should_roundoff:
            subtotal = Decimal('0')
            for line in document_lines:
                qty = Decimal(str(line.get("Quantity", 0)))
                price = Decimal(str(line.get("UnitPrice", 0)))
                subtotal += qty * price
            if subtotal > 0:
                rounded = subtotal.quantize(Decimal('1'), rounding='ROUND_HALF_UP')
                round_dif = float(rounded - subtotal)
                if round_dif != 0:
                    grpo_payload["RoundDif"] = round_dif

        if vendor_ref:
            grpo_payload["NumAtCard"] = vendor_ref

        # Extra charges (DocumentAdditionalExpenses)
        if extra_charges:
            additional_expenses = []
            for charge in extra_charges:
                expense = {
                    "ExpenseCode": charge["expense_code"],
                    "LineTotal": float(charge["amount"]),
                }
                if charge.get("remarks"):
                    expense["Remarks"] = charge["remarks"]
                if charge.get("tax_code"):
                    expense["TaxCode"] = charge["tax_code"]
                additional_expenses.append(expense)
            grpo_payload["DocumentAdditionalExpenses"] = additional_expenses

        # Upload attachments to SAP BEFORE creating GRPO
        sap_client = SAPClient(company_code=self.company_code)
        attachment_records = []
        sap_absolute_entry = None

        if attachments:
            for uploaded_file in attachments:
                suffix = os.path.splitext(uploaded_file.name)[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    for chunk in uploaded_file.chunks():
                        tmp.write(chunk)
                    tmp_path = tmp.name

                try:
                    sap_result = sap_client.upload_attachment(
                        file_path=tmp_path,
                        filename=uploaded_file.name,
                        allow_metadata_fallback=True,
                    )
                    abs_entry = sap_result.get("AbsoluteEntry")
                    if abs_entry:
                        sap_absolute_entry = abs_entry
                        attachment_records.append({
                            "file": uploaded_file,
                            "filename": uploaded_file.name,
                            "sap_absolute_entry": abs_entry,
                        })
                        logger.info(
                            f"Attachment '{uploaded_file.name}' uploaded to SAP. "
                            f"AbsoluteEntry: {abs_entry}"
                        )
                finally:
                    os.unlink(tmp_path)

            if sap_absolute_entry:
                grpo_payload["AttachmentEntry"] = sap_absolute_entry

        po_numbers_str = ", ".join(po.po_number for po in po_receipts)
        logger.info(f"GRPO Payload for PO(s) {po_numbers_str}: {grpo_payload}")

        # Post to SAP
        try:
            result = sap_client.create_grpo(grpo_payload)

            grpo_posting.sap_doc_entry = result.get("DocEntry")
            grpo_posting.sap_doc_num = result.get("DocNum")
            grpo_posting.sap_doc_total = Decimal(str(result.get("DocTotal", 0)))
            grpo_posting.status = GRPOStatus.POSTED
            grpo_posting.posted_at = timezone.now()
            grpo_posting.posted_by = user
            grpo_posting.save()

            for line_data in grpo_lines_data:
                GRPOLinePosting.objects.create(
                    grpo_posting=grpo_posting,
                    po_item_receipt=line_data["po_item_receipt"],
                    quantity_posted=line_data["quantity_posted"],
                    base_entry=line_data["base_entry"],
                    base_line=line_data["base_line"],
                )

            for att_data in attachment_records:
                GRPOAttachment.objects.create(
                    grpo_posting=grpo_posting,
                    file=att_data["file"],
                    original_filename=att_data["filename"],
                    sap_attachment_status=SAPAttachmentStatus.LINKED,
                    sap_absolute_entry=att_data["sap_absolute_entry"],
                    uploaded_by=user,
                )

            logger.info(
                f"GRPO posted successfully for PO(s) {po_numbers_str}. "
                f"SAP DocNum: {grpo_posting.sap_doc_num}"
            )

            return grpo_posting

        except SAPValidationError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = str(e)
            grpo_posting.save()
            logger.error(f"SAP validation error posting GRPO: {e}")
            raise

        except SAPConnectionError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = "SAP system unavailable"
            grpo_posting.save()
            logger.error(f"SAP connection error posting GRPO: {e}")
            raise

        except SAPDataError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = str(e)
            grpo_posting.save()
            logger.error(f"SAP data error posting GRPO: {e}")
            raise

    def get_pending_service_grpo_entries(self) -> List[DispatchPlan]:
        """
        Get booked dispatch plans pending transport service GRPO posting.
        A plan appears here only after the transport booking is marked BOOKED.
        """
        plans = list(
            DispatchPlan.objects.filter(
                company__code=self.company_code,
                booking_status=DispatchPlanStatus.BOOKED,
                is_active=True,
            )
            .select_related(
                "company",
                "vehicle",
                "vehicle__transporter",
                "transporter",
                "driver",
                "linked_vehicle_entry",
                "linked_vehicle_entry__vehicle",
                "linked_vehicle_entry__vehicle__transporter",
                "linked_vehicle_entry__driver",
            )
            .prefetch_related("service_grpo_postings", "service_grpo_lines")
            .distinct()
            .order_by("-updated_at", "-created_at")
        )
        posted_plan_ids = set(
            DispatchPlan.objects.filter(
                company__code=self.company_code,
                is_active=True,
            )
            .filter(
                Q(service_grpo_postings__status=GRPOStatus.POSTED)
                | Q(service_grpo_lines__service_grpo_posting__status=GRPOStatus.POSTED)
            )
            .values_list("id", flat=True)
        )

        grouped = {}
        posted_group_keys = set()
        group_counts = {}
        for plan in plans:
            key = self._service_group_key(plan)
            group_counts[key] = group_counts.get(key, 0) + 1
            if plan.id in posted_plan_ids:
                posted_group_keys.add(key)
                grouped.pop(key, None)
                continue
            if key in posted_group_keys:
                continue
            if key not in grouped:
                grouped[key] = plan

        result = []
        for key, plan in grouped.items():
            setattr(plan, "_service_group_invoice_count", group_counts.get(key, 1))
            result.append(plan)
        return result

    @staticmethod
    def _get_dispatch_bilty_attachment_name(dispatch_plan: DispatchPlan) -> str:
        if not dispatch_plan.bilty_attachment:
            return ""
        filename = (
            dispatch_plan.bilty_attachment_name
            or os.path.basename(dispatch_plan.bilty_attachment.name)
        )
        return filename or "bilty_attachment"

    def get_service_grpo_preview_data(self, dispatch_plan_id: int) -> Dict[str, Any]:
        """Get dispatch booking data required for service GRPO posting."""
        try:
            dispatch_plan = (
                DispatchPlan.objects.select_related(
                    "company",
                    "vehicle",
                    "vehicle__transporter",
                    "transporter",
                    "driver",
                    "linked_vehicle_entry",
                    "linked_vehicle_entry__vehicle",
                    "linked_vehicle_entry__vehicle__transporter",
                    "linked_vehicle_entry__driver",
                )
                .prefetch_related("service_grpo_postings")
                .get(
                    id=dispatch_plan_id,
                    company__code=self.company_code,
                    is_active=True,
                )
            )
        except DispatchPlan.DoesNotExist:
            raise ValueError(f"Dispatch plan {dispatch_plan_id} not found")

        group_plans = self._get_service_group_plans(dispatch_plan)
        existing_grpo = self._posted_service_grpo_for_group(group_plans)
        is_ready = (
            all(plan.booking_status == DispatchPlanStatus.BOOKED for plan in group_plans)
            and existing_grpo is None
        )

        line_amounts = [self._line_amount_from_plan(plan) for plan in group_plans]
        amount = sum(line_amounts, Decimal("0.00"))

        vehicle_no = self.get_service_display_vehicle_no(dispatch_plan)
        driver_name = self.get_service_display_driver_name(dispatch_plan)
        transporter_name = self._dispatch_transporter_name(dispatch_plan)
        transporter_gstin = self._dispatch_transporter_gstin(dispatch_plan)

        latest_grpo = self._latest_service_grpo_for_group(group_plans)
        bill_snapshot = self._get_dispatch_bill_snapshot(dispatch_plan)
        item_summary = bill_snapshot.get("item_summary", "")
        inferred_product_variety = self._infer_product_variety(item_summary)
        product_variety = (
            inferred_product_variety
            or dispatch_plan.product_variety
        )
        service_description = self._infer_service_description(
            item_summary,
            product_variety,
        )
        product_dimension = self._resolve_product_dimension_code(
            item_summary,
            product_variety,
            service_description,
        )
        delivery_point = self._infer_budget_delivery_point(dispatch_plan)
        source_state = bill_snapshot.get("state", "") or dispatch_plan.place_of_supply
        invoice_lines = []
        total_litres = Decimal("0.000")
        invoice_amount_total = Decimal("0.00")
        for index, plan in enumerate(group_plans):
            line_snapshot = self._get_dispatch_bill_snapshot(plan)
            line_item_summary = line_snapshot.get("item_summary", "")
            inferred_line_product_variety = self._infer_product_variety(line_item_summary)
            line_product_variety = (
                inferred_line_product_variety
                or plan.product_variety
            )
            line_service_description = self._infer_service_description(
                line_item_summary,
                line_product_variety,
            )[:255]
            line_product_dimension = self._resolve_product_dimension_code(
                line_item_summary,
                line_product_variety,
                line_service_description,
            )
            line_total_litres = plan.total_litres
            if line_total_litres is None and line_snapshot:
                line_total_litres = self._decimal_or_none(
                    line_snapshot.get("total_litres"), "0.001"
                )
            line_invoice_weight = plan.invoice_weight
            if line_invoice_weight is None and line_snapshot:
                line_invoice_weight = self._decimal_or_none(
                    line_snapshot.get("total_weight"), "0.001"
                )
            line_invoice_amount = plan.invoice_amount
            if line_invoice_amount is None and line_snapshot:
                line_invoice_amount = self._decimal_or_none(
                    line_snapshot.get("doc_total"), "0.01"
                )
            if line_total_litres is not None:
                total_litres += Decimal(str(line_total_litres))
            if line_invoice_amount is not None:
                invoice_amount_total += Decimal(str(line_invoice_amount))

            invoice_lines.append(
                {
                    "dispatch_plan_id": plan.id,
                    "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
                    "sap_invoice_doc_num": plan.sap_invoice_doc_num,
                    "invoice_number": plan.invoice_number
                    or str(line_snapshot.get("doc_num") or ""),
                    "customer_code": line_snapshot.get("card_code", ""),
                    "customer_name": line_snapshot.get("card_name", ""),
                    "source_state": line_snapshot.get("state", "") or plan.place_of_supply,
                    "source_city": line_snapshot.get("city", ""),
                    "service_description": line_service_description,
                    "product_variety": line_product_variety,
                    "product_dimension": line_product_dimension,
                    "total_litres": line_total_litres,
                    "invoice_weight": line_invoice_weight,
                    "invoice_amount": line_invoice_amount,
                    "freight_amount": line_amounts[index],
                }
            )
        total_litres = total_litres if total_litres != Decimal("0.000") else None
        invoice_weight = dispatch_plan.invoice_weight
        invoice_amount = dispatch_plan.invoice_amount
        if invoice_amount is None and bill_snapshot:
            invoice_amount = self._decimal_or_none(
                bill_snapshot.get("doc_total"), "0.01"
            )
        if len(group_plans) > 1:
            invoice_weight = None
            invoice_amount = None
        effective_month = dispatch_plan.effective_month or self._first_day_of_month(
            dispatch_plan.dispatch_date or bill_snapshot.get("doc_date")
        )

        return {
            "dispatch_plan_id": dispatch_plan.id,
            "sap_invoice_doc_entry": dispatch_plan.sap_invoice_doc_entry,
            "sap_invoice_doc_num": dispatch_plan.sap_invoice_doc_num,
            "booking_status": dispatch_plan.booking_status,
            "dispatch_date": dispatch_plan.dispatch_date,
            "is_ready_for_grpo": is_ready,
            "linked_vehicle_entry_id": dispatch_plan.linked_vehicle_entry_id,
            "linked_vehicle_entry_no": self.get_service_display_linked_entry_no(dispatch_plan),
            "vehicle_no": vehicle_no,
            "driver_name": driver_name,
            "transporter_name": transporter_name,
            "transporter_gstin": transporter_gstin,
            "bilty_no": dispatch_plan.bilty_no,
            "bilty_date": dispatch_plan.bilty_date,
            "freight": dispatch_plan.freight,
            "total_freight": dispatch_plan.total_freight,
            "invoice_count": len(group_plans),
            "created_at": dispatch_plan.created_at,
            "updated_at": dispatch_plan.updated_at,
            "default_amount": amount,
            "default_service_description": service_description[:255],
            "default_place_of_supply": (
                "" if len(group_plans) > 1 else dispatch_plan.place_of_supply or source_state or "HR"
            ),
            "default_effective_month": effective_month,
            "default_budget_delivery_point": delivery_point,
            "default_location_code": dispatch_plan.service_location_code,
            "default_location_name": dispatch_plan.service_location_name,
            "default_sac_entry": dispatch_plan.sac_entry,
            "default_sac_code": dispatch_plan.sac_code,
            "default_product_variety": product_variety,
            "default_product_dimension": product_dimension,
            "default_total_litres": total_litres,
            "default_sub_account": self._infer_service_sub_account(bill_snapshot),
            "invoice_number": (
                "" if len(group_plans) > 1 else dispatch_plan.invoice_number
                or str(bill_snapshot.get("doc_num") or "")
            ),
            "eway_bill": (
                "" if len(group_plans) > 1 else dispatch_plan.eway_bill
                or bill_snapshot.get("sap_eway_bill", "")
            ),
            "invoice_weight": invoice_weight,
            "invoice_amount": invoice_amount,
            "source_state": source_state,
            "source_city": bill_snapshot.get("city", ""),
            "item_summary": item_summary,
            "bilty_attachment": (
                dispatch_plan.bilty_attachment.url
                if dispatch_plan.bilty_attachment
                else None
            ),
            "bilty_attachment_name": self._get_dispatch_bilty_attachment_name(
                dispatch_plan
            ),
            "grpo_status": existing_grpo.status if existing_grpo else (
                latest_grpo.status if latest_grpo else None
            ),
            "sap_doc_num": existing_grpo.sap_doc_num if existing_grpo else (
                latest_grpo.sap_doc_num if latest_grpo else None
            ),
            "total_amount": existing_grpo.sap_doc_total if existing_grpo else (
                latest_grpo.sap_doc_total if latest_grpo else None
            ),
            "invoice_lines": invoice_lines,
        }

    def _build_service_structured_comments(
        self,
        user,
        dispatch_plan: DispatchPlan,
        user_comments: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build the SAP service GRPO document remarks (Comments field).

        Matches the convention used across the Jivo Oil company DB, where the
        document Comments/Remarks footer carries the bilty number, e.g.
        "BILTY NO 6805". Falls back to an app/user audit string only when no
        bilty number is available so the field is never left blank.
        """
        parts = []
        if dispatch_plan.bilty_no:
            parts.append(f"BILTY NO {dispatch_plan.bilty_no}")
        if user_comments:
            parts.append(user_comments.strip())

        comments = " | ".join(part for part in parts if part)
        if not comments:
            full_name = user.get_full_name() if hasattr(user, "get_full_name") else str(user)
            username = getattr(user, "username", getattr(user, "email", str(user)))
            comments = f"App: JI | User: {full_name} ({username})"

        return self._truncate_sap_document_comments(comments)

    def _get_service_attachment_sources(
        self,
        dispatch_plan: DispatchPlan,
        attachments: Optional[list],
        include_bilty_attachment: bool,
    ) -> List[Dict[str, Any]]:
        sources = []
        if include_bilty_attachment and dispatch_plan.bilty_attachment:
            sources.append(
                {
                    "kind": "dispatch_bilty",
                    "file": dispatch_plan.bilty_attachment,
                    "filename": self._get_dispatch_bilty_attachment_name(dispatch_plan),
                }
            )

        for uploaded_file in attachments or []:
            sources.append(
                {
                    "kind": "uploaded",
                    "file": uploaded_file,
                    "filename": uploaded_file.name,
                }
            )
        return sources

    @staticmethod
    def _copy_attachment_to_temp(file_obj, filename: str) -> str:
        suffix = os.path.splitext(filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            if hasattr(file_obj, "seek"):
                try:
                    file_obj.seek(0)
                except Exception:
                    pass
            if hasattr(file_obj, "chunks"):
                for chunk in file_obj.chunks():
                    tmp.write(chunk)
            else:
                while True:
                    chunk = file_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    tmp.write(chunk)
            tmp_path = tmp.name

        if hasattr(file_obj, "seek"):
            try:
                file_obj.seek(0)
            except Exception:
                pass
        return tmp_path

    @staticmethod
    def _create_service_attachment_record(
        grpo_posting: ServiceGRPOPosting,
        att_data: Dict[str, Any],
        user,
    ) -> None:
        source_file = att_data["file"]
        filename = att_data["filename"]

        if att_data["kind"] == "dispatch_bilty":
            attachment = ServiceGRPOAttachment(
                service_grpo_posting=grpo_posting,
                original_filename=filename,
                sap_attachment_status=SAPAttachmentStatus.LINKED,
                sap_absolute_entry=att_data["sap_absolute_entry"],
                uploaded_by=user,
            )
            source_file.open("rb")
            try:
                attachment.file.save(filename, File(source_file), save=False)
            finally:
                source_file.close()
            attachment.save()
            return

        if hasattr(source_file, "seek"):
            source_file.seek(0)
        ServiceGRPOAttachment.objects.create(
            service_grpo_posting=grpo_posting,
            file=source_file,
            original_filename=filename,
            sap_attachment_status=SAPAttachmentStatus.LINKED,
            sap_absolute_entry=att_data["sap_absolute_entry"],
            uploaded_by=user,
        )

    @transaction.atomic
    def post_service_grpo(
        self,
        dispatch_plan_id: int,
        user,
        vendor_code: str,
        branch_id: int,
        service_description: str,
        amount: Decimal,
        tax_code: Optional[str] = None,
        gl_account: Optional[str] = None,
        unit_price: Optional[Decimal] = None,
        place_of_supply: Optional[str] = None,
        effective_month: Optional[str] = None,
        budget_delivery_point: Optional[str] = None,
        sub_account: Optional[str] = None,
        location_code: Optional[int] = None,
        location_name: Optional[str] = None,
        sac_entry: Optional[int] = None,
        sac_code: Optional[str] = None,
        product_variety: Optional[str] = None,
        total_litres: Optional[Decimal] = None,
        invoice_number: Optional[str] = None,
        eway_bill: Optional[str] = None,
        invoice_weight: Optional[Decimal] = None,
        invoice_amount: Optional[Decimal] = None,
        comments: Optional[str] = None,
        vendor_ref: Optional[str] = None,
        extra_charges: Optional[List[Dict[str, Any]]] = None,
        attachments: Optional[list] = None,
        include_bilty_attachment: bool = True,
        doc_date: Optional[str] = None,
        doc_due_date: Optional[str] = None,
        tax_date: Optional[str] = None,
        should_roundoff: bool = False,
    ) -> ServiceGRPOPosting:
        """
        Post a service-type GRPO to SAP for a booked dispatch transport plan.
        The SAP document is a PurchaseDeliveryNotes document with service lines.
        """
        try:
            dispatch_plan = DispatchPlan.objects.select_related(
                "company",
                "vehicle",
                "vehicle__transporter",
                "transporter",
                "driver",
                "linked_vehicle_entry",
                "linked_vehicle_entry__vehicle",
                "linked_vehicle_entry__vehicle__transporter",
                "linked_vehicle_entry__driver",
            ).get(
                id=dispatch_plan_id,
                company__code=self.company_code,
                is_active=True,
            )
        except DispatchPlan.DoesNotExist:
            raise ValueError(f"Dispatch plan {dispatch_plan_id} not found")

        group_plans = self._get_service_group_plans(dispatch_plan)
        if any(plan.booking_status != DispatchPlanStatus.BOOKED for plan in group_plans):
            raise ValueError(
                "Service GRPO can be posted only after the vehicle booking is Booked."
            )

        existing_grpo = self._posted_service_grpo_for_group(group_plans)
        if existing_grpo:
            raise ValueError(
                f"Service GRPO already posted for this bilty group. "
                f"SAP Doc Num: {existing_grpo.sap_doc_num}"
            )

        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Service amount must be greater than zero.")

        vendor_code = (vendor_code or "").strip()
        if not vendor_code:
            raise ValueError("SAP vendor code is required.")

        service_description = (
            service_description
            or f"Transport freight for dispatch bill {dispatch_plan.sap_invoice_doc_num}"
        ).strip()[:255]
        if not service_description:
            raise ValueError("Service description is required.")

        unit_price = Decimal(str(unit_price)) if unit_price is not None else amount
        place_of_supply = (place_of_supply or "").strip()
        budget_delivery_point = (budget_delivery_point or "").strip()
        sub_account = (sub_account or "").strip()
        location_name = (location_name or "").strip()
        sac_code = (sac_code or "").strip()
        product_variety = (product_variety or "").strip()
        invoice_number = (invoice_number or "").strip()
        eway_bill = (eway_bill or "").strip()
        total_litres = (
            Decimal(str(total_litres)) if total_litres not in (None, "") else None
        )
        invoice_weight = (
            Decimal(str(invoice_weight)) if invoice_weight not in (None, "") else None
        )
        invoice_amount = (
            Decimal(str(invoice_amount)) if invoice_amount not in (None, "") else None
        )

        if effective_month:
            effective_month = self._first_day_of_month(effective_month)
        if not effective_month:
            raise ValueError("Expense Effective Month is required for Service GRPO.")
        effective_month_dimension = self._resolve_active_dimension_code(
            2,
            self._format_effective_month_dimension(effective_month),
        )
        if not effective_month_dimension:
            raise ValueError(
                "Expense Effective Month is not configured as an active SAP dimension code."
            )
        post_budget_as_dimension = self._is_active_budget_code(budget_delivery_point)
        bill_snapshot = self._get_dispatch_bill_snapshot(dispatch_plan)
        inferred_product_variety = self._infer_product_variety(
            bill_snapshot.get("item_summary", "")
        )
        if not product_variety and inferred_product_variety:
            product_variety = inferred_product_variety
        group_line_data = []
        aggregate_litres = Decimal("0.000")
        aggregate_invoice_amount = Decimal("0.00")
        line_weights = []

        for plan in group_plans:
            line_snapshot = self._get_dispatch_bill_snapshot(plan)
            line_item_summary = line_snapshot.get("item_summary", "")
            inferred_line_product_variety = self._infer_product_variety(line_item_summary)
            line_product_variety = (
                plan.product_variety
                or product_variety
                or inferred_line_product_variety
            )
            line_service_description = self._infer_service_description(
                line_item_summary,
                line_product_variety,
            )
            line_total_litres = plan.total_litres
            if line_total_litres is None and line_snapshot:
                line_total_litres = self._decimal_or_none(
                    line_snapshot.get("total_litres"), "0.001"
                )
            line_invoice_weight = plan.invoice_weight
            if line_invoice_weight is None and line_snapshot:
                line_invoice_weight = self._decimal_or_none(
                    line_snapshot.get("total_weight"), "0.001"
                )
            line_invoice_amount = plan.invoice_amount
            if line_invoice_amount is None and line_snapshot:
                line_invoice_amount = self._decimal_or_none(
                    line_snapshot.get("doc_total"), "0.01"
                )

            if len(group_plans) == 1 and total_litres is not None:
                line_total_litres = total_litres
            if line_total_litres is not None:
                aggregate_litres += Decimal(str(line_total_litres))
            if line_invoice_amount is not None:
                aggregate_invoice_amount += Decimal(str(line_invoice_amount))

            weight = Decimal(str(line_total_litres or 0))
            if weight <= 0:
                weight = Decimal(str(line_invoice_weight or 0))
            if weight <= 0:
                weight = Decimal(str(line_invoice_amount or 0))
            line_weights.append(weight)
            group_line_data.append(
                {
                    "plan": plan,
                    "snapshot": line_snapshot,
                    "source_state": (
                        line_snapshot.get("state")
                        or plan.place_of_supply
                        or place_of_supply
                    ),
                    "product_variety": line_product_variety,
                    "total_litres": line_total_litres,
                    "invoice_weight": line_invoice_weight,
                    "invoice_amount": line_invoice_amount,
                    "service_description": (line_service_description or service_description)[:255],
                }
            )

        saved_line_amounts = [self._line_amount_from_plan(plan) for plan in group_plans]
        saved_amount_total = sum(saved_line_amounts, Decimal("0.00"))
        if len(group_plans) == 1:
            line_amounts = [amount]
        elif saved_amount_total > 0 and abs(saved_amount_total - amount) <= Decimal("0.01"):
            line_amounts = saved_line_amounts
        else:
            line_amounts = self._allocate_amount_by_weight(amount, line_weights)

        if total_litres is None and aggregate_litres > 0:
            total_litres = aggregate_litres
        if invoice_amount is None and aggregate_invoice_amount > 0:
            invoice_amount = aggregate_invoice_amount

        branch_state = self._get_sap_branch_states(self.company_code).get(int(branch_id), "")
        vendor_state = self._get_sap_bp_state(self.company_code, vendor_code)
        effective_place_of_supply = vendor_state or place_of_supply
        tax_supply_state = effective_place_of_supply
        for line_data in group_line_data:
            line_snapshot = line_data["snapshot"]
            product_dimension = self._resolve_product_dimension_code(
                line_snapshot.get("item_summary", ""),
                line_data["product_variety"],
                line_data["service_description"],
            )
            if not product_dimension:
                variety_label = (
                    line_data["product_variety"]
                    or line_data["service_description"]
                    or "blank"
                )
                raise ValueError(
                    "SAP Variety is required for Service GRPO. "
                    f"Could not resolve '{variety_label}' to an active SAP Variety "
                    "distribution rule."
                )
            line_data["product_dimension"] = product_dimension
        posting_vehicle_no = self._dispatch_vehicle_no(dispatch_plan)
        posting_transporter_name = self._dispatch_transporter_name(dispatch_plan)

        grpo_posting = ServiceGRPOPosting.objects.create(
            dispatch_plan=dispatch_plan,
            vendor_code=vendor_code,
            vendor_name=posting_transporter_name,
            place_of_supply=effective_place_of_supply,
            effective_month=effective_month,
            budget_delivery_point=budget_delivery_point,
            sub_account=sub_account,
            location_code=location_code,
            location_name=location_name,
            sac_entry=sac_entry,
            sac_code=sac_code,
            product_variety=product_variety,
            total_litres=total_litres,
            status=GRPOStatus.PENDING,
            posted_by=user,
        )

        company_code = self.company_code.upper()
        document_lines = []
        for index, line_data in enumerate(group_line_data):
            plan = line_data["plan"]
            line_snapshot = line_data["snapshot"]
            line_amount = line_amounts[index]
            line_unit_price = line_amount
            line_invoice_number = (
                plan.invoice_number
                or str(line_snapshot.get("doc_num") or "")
                or invoice_number
            )
            line_total_litres = line_data["total_litres"]
            line_tax_code = self._resolve_service_line_tax_code(
                requested_tax_code=tax_code,
                branch_state=branch_state,
                supply_state=tax_supply_state,
            )
            line_data["tax_code"] = line_tax_code
            product_dimension = line_data["product_dimension"]
            state_dimension = self._resolve_active_dimension_code(
                5,
                self._normalize_state(line_data["source_state"]),
            )

            document_line = {
                "ItemDescription": line_data["service_description"],
                "LineTotal": float(line_amount),
                "UnitPrice": float(line_unit_price),
                "U_UNE_SCHI": "N",
                "U_UNE_CUNT": "Y",
            }
            if company_code != "JIVO_MART":
                document_line["U_UNE_CALI"] = "Y"
            if gl_account:
                document_line["AccountCode"] = gl_account
            if line_tax_code:
                document_line["TaxCode"] = line_tax_code
            if sac_entry:
                document_line["SACEntry"] = int(sac_entry)
            if location_code:
                document_line["LocationCode"] = int(location_code)
            if product_dimension:
                document_line["CostingCode"] = product_dimension
            if line_data["product_variety"]:
                document_line["U_Variety"] = line_data["product_variety"]
            if effective_month_dimension:
                document_line["CostingCode2"] = effective_month_dimension
            if post_budget_as_dimension:
                document_line["CostingCode3"] = budget_delivery_point
            if state_dimension:
                document_line["CostingCode5"] = state_dimension
            if sub_account:
                document_line["U_Sub_Account"] = sub_account
            customer_code = (line_snapshot.get("card_code") or "").strip()
            if customer_code:
                document_line["U_CardCode"] = customer_code
            if line_total_litres is not None:
                document_line["U_UNE_LTS"] = float(line_total_litres)
                if company_code == "JIVO_MART":
                    document_line["U_Disp_Qty"] = float(line_total_litres)
                    document_line["U_Recvd_Qty"] = float(line_total_litres)
            if dispatch_plan.bilty_no:
                document_line["U_BilltyNumber"] = dispatch_plan.bilty_no
            if company_code == "JIVO_BEVERAGES" and dispatch_plan.bilty_date:
                document_line["U_BiltyDate"] = dispatch_plan.bilty_date.isoformat()
            if line_invoice_number:
                document_line["U_ARNO"] = line_invoice_number
            if line_data["product_variety"]:
                document_line["U_Variety"] = line_data["product_variety"][:50]

            # Match SAP convention: line remarks carry only the bilty number,
            # e.g. "BILTY NO 6805".
            if dispatch_plan.bilty_no:
                document_line["U_Remarks"] = f"BILTY NO {dispatch_plan.bilty_no}"[:254]
            document_lines.append(document_line)

        structured_comments = self._build_service_structured_comments(user, dispatch_plan)

        is_multi_invoice = len(group_plans) > 1
        grpo_payload = {
            "DocType": "dDocument_Service",
            "CardCode": vendor_code,
            "BPL_IDAssignedToInvoice": branch_id,
            "Comments": structured_comments,
            "DocumentLines": document_lines,
        }
        if effective_place_of_supply and not is_multi_invoice:
            grpo_payload["ShipPlace"] = effective_place_of_supply
        if dispatch_plan.bilty_no:
            grpo_payload["U_BilltyNumber"] = dispatch_plan.bilty_no
            grpo_payload["U_LRNUmber"] = dispatch_plan.bilty_no
        if dispatch_plan.bilty_date:
            grpo_payload["U_BiltyDate"] = dispatch_plan.bilty_date.isoformat()
        if posting_transporter_name:
            grpo_payload["U_TransporterName"] = posting_transporter_name
        if posting_vehicle_no:
            grpo_payload["U_VehicleNoM"] = posting_vehicle_no
        if invoice_number and not is_multi_invoice:
            grpo_payload["U_ARNO"] = invoice_number
            grpo_payload["U_TransporterInvoice"] = invoice_number
        if invoice_amount is not None and not is_multi_invoice:
            grpo_payload["U_TotalAmt"] = float(invoice_amount)

        if doc_date:
            grpo_payload["DocDate"] = str(doc_date)
        if doc_due_date:
            grpo_payload["DocDueDate"] = str(doc_due_date)
        if tax_date:
            grpo_payload["TaxDate"] = str(tax_date)
        if vendor_ref:
            grpo_payload["NumAtCard"] = vendor_ref

        subtotal = amount
        if extra_charges:
            additional_expenses = []
            for charge in extra_charges:
                charge_amount = Decimal(str(charge["amount"]))
                subtotal += charge_amount
                expense = {
                    "ExpenseCode": charge["expense_code"],
                    "LineTotal": float(charge_amount),
                }
                if charge.get("remarks"):
                    expense["Remarks"] = charge["remarks"]
                if charge.get("tax_code"):
                    expense["TaxCode"] = self._resolve_service_line_tax_code(
                        requested_tax_code=charge["tax_code"],
                        branch_state=branch_state,
                        supply_state=tax_supply_state,
                    )
                additional_expenses.append(expense)
            grpo_payload["DocumentAdditionalExpenses"] = additional_expenses

        if should_roundoff and subtotal > 0:
            rounded = subtotal.quantize(Decimal("1"), rounding="ROUND_HALF_UP")
            round_dif = float(rounded - subtotal)
            if round_dif != 0:
                grpo_payload["RoundDif"] = round_dif

        sap_client = SAPClient(company_code=self.company_code)
        attachment_records = []
        sap_absolute_entry = None
        attachment_sources = self._get_service_attachment_sources(
            dispatch_plan=dispatch_plan,
            attachments=attachments,
            include_bilty_attachment=include_bilty_attachment,
        )

        if attachment_sources:
            for attachment_source in attachment_sources:
                filename = attachment_source["filename"]
                file_obj = attachment_source["file"]
                tmp_path = self._copy_attachment_to_temp(file_obj, filename)
                try:
                    if sap_absolute_entry:
                        sap_client.add_line_to_existing_attachment(
                            absolute_entry=sap_absolute_entry,
                            file_path=tmp_path,
                            filename=filename,
                        )
                        abs_entry = sap_absolute_entry
                    else:
                        sap_result = sap_client.upload_attachment(
                            file_path=tmp_path,
                            filename=filename,
                        )
                        abs_entry = sap_result.get("AbsoluteEntry")
                    if abs_entry:
                        sap_absolute_entry = abs_entry
                        attachment_records.append(
                            {
                                **attachment_source,
                                "sap_absolute_entry": abs_entry,
                            }
                        )
                finally:
                    os.unlink(tmp_path)

            if sap_absolute_entry:
                grpo_payload["AttachmentEntry"] = sap_absolute_entry

        self._filter_purchase_delivery_note_udfs(grpo_payload)

        logger.info(
            "Service GRPO payload for dispatch plan %s: %s",
            dispatch_plan.id,
            grpo_payload,
        )

        try:
            result = sap_client.create_grpo(grpo_payload)

            grpo_posting.sap_doc_entry = result.get("DocEntry")
            grpo_posting.sap_doc_num = result.get("DocNum")
            grpo_posting.sap_doc_total = Decimal(str(result.get("DocTotal", 0)))
            grpo_posting.status = GRPOStatus.POSTED
            grpo_posting.posted_at = timezone.now()
            grpo_posting.posted_by = user
            grpo_posting.save()

            for index, line_data in enumerate(group_line_data):
                ServiceGRPOLinePosting.objects.create(
                    service_grpo_posting=grpo_posting,
                    dispatch_plan=line_data["plan"],
                    service_description=line_data["service_description"],
                    amount=line_amounts[index],
                    unit_price=line_amounts[index],
                    tax_code=line_data.get("tax_code") or tax_code or "",
                    gl_account=gl_account or "",
                    sac_entry=sac_entry,
                    sac_code=sac_code,
                    location_code=location_code,
                    location_name=location_name,
                    project_code=budget_delivery_point,
                    sub_account=sub_account,
                    product_variety=line_data["product_variety"],
                    total_litres=line_data["total_litres"],
                )

            for att_data in attachment_records:
                self._create_service_attachment_record(
                    grpo_posting=grpo_posting,
                    att_data=att_data,
                    user=user,
                )

            logger.info(
                "Service GRPO posted for dispatch plan %s. SAP DocNum: %s",
                dispatch_plan.id,
                grpo_posting.sap_doc_num,
            )
            return grpo_posting

        except SAPValidationError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = str(e)
            grpo_posting.save()
            logger.error(f"SAP validation error posting service GRPO: {e}")
            raise

        except SAPConnectionError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = "SAP system unavailable"
            grpo_posting.save()
            logger.error(f"SAP connection error posting service GRPO: {e}")
            raise

        except SAPDataError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = str(e)
            grpo_posting.save()
            logger.error(f"SAP data error posting service GRPO: {e}")
            raise

    def get_service_grpo_posting_history(
        self,
        dispatch_plan_id: Optional[int] = None,
    ) -> List[ServiceGRPOPosting]:
        """Get service GRPO posting history."""
        queryset = ServiceGRPOPosting.objects.select_related(
            "dispatch_plan",
            "dispatch_plan__vehicle",
            "dispatch_plan__vehicle__transporter",
            "dispatch_plan__transporter",
            "dispatch_plan__linked_vehicle_entry",
            "dispatch_plan__linked_vehicle_entry__vehicle",
            "dispatch_plan__linked_vehicle_entry__vehicle__transporter",
            "dispatch_plan__linked_vehicle_entry__driver",
            "posted_by",
        ).prefetch_related("lines", "attachments").filter(
            dispatch_plan__company__code=self.company_code,
        )

        if dispatch_plan_id:
            queryset = queryset.filter(dispatch_plan_id=dispatch_plan_id)

        return queryset.order_by("-created_at")

    def get_service_grpo_options(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get SAP master-data options used by the service GRPO form."""
        sap_client = SAPClient(company_code=self.company_code)
        return sap_client.get_service_grpo_options()

    def get_grpo_posting_history(
        self,
        vehicle_entry_id: Optional[int] = None
    ) -> List[GRPOPosting]:
        """Get GRPO posting history."""
        queryset = GRPOPosting.objects.select_related(
            "vehicle_entry",
            "po_receipt",
            "posted_by"
        ).prefetch_related("lines", "attachments", "po_receipts").filter(
            vehicle_entry__company__code=self.company_code,
            vehicle_entry__is_active=True,
        )

        if vehicle_entry_id:
            queryset = queryset.filter(vehicle_entry_id=vehicle_entry_id)

        return queryset.order_by("-created_at")

    def upload_grpo_attachment(
        self,
        grpo_posting_id: int,
        file,
        user
    ) -> GRPOAttachment:
        """
        Upload an attachment for a GRPO posting.
        1. Save file locally (via Django FileField)
        2. Upload to SAP Attachments2 endpoint
        3. Link to the GRPO document via PATCH
        4. Update local record with SAP response
        """
        # Validate GRPO posting exists and is POSTED
        try:
            grpo_posting = GRPOPosting.objects.get(id=grpo_posting_id)
        except GRPOPosting.DoesNotExist:
            raise ValueError(f"GRPO posting {grpo_posting_id} not found")

        if grpo_posting.status != GRPOStatus.POSTED:
            raise ValueError(
                f"Cannot attach files to GRPO with status '{grpo_posting.status}'. "
                f"Only POSTED GRPOs accept attachments."
            )

        if not grpo_posting.sap_doc_entry:
            raise ValueError("GRPO posting has no SAP DocEntry. Cannot upload attachment.")

        # Step 1: Save file locally
        attachment = GRPOAttachment.objects.create(
            grpo_posting=grpo_posting,
            file=file,
            original_filename=file.name,
            sap_attachment_status=SAPAttachmentStatus.PENDING,
            uploaded_by=user,
        )

        # Step 2: Upload to SAP
        try:
            sap_client = SAPClient(company_code=self.company_code)

            # Check if the GRPO already has an AttachmentEntry
            existing_abs_entry = sap_client.get_grpo_attachment_entry(
                grpo_posting.sap_doc_entry
            )
            existing_abs_entry = self._sap_absolute_entry_or_none(existing_abs_entry)

            if existing_abs_entry:
                # Add a new line to the existing Attachments2 entry.
                # This avoids PATCHing the GRPO document which triggers
                # SAP approval error (200039).
                sap_result = sap_client.add_line_to_existing_attachment(
                    absolute_entry=existing_abs_entry,
                    file_path=attachment.file.path,
                    filename=attachment.original_filename,
                    allow_metadata_fallback=True,
                )
                attachment.sap_absolute_entry = existing_abs_entry
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.save(update_fields=[
                    "sap_absolute_entry", "sap_attachment_status"
                ])
            else:
                # No existing attachment — upload and include in GRPO
                sap_result = sap_client.upload_attachment(
                    file_path=attachment.file.path,
                    filename=attachment.original_filename,
                    allow_metadata_fallback=True,
                )
                absolute_entry = self._sap_absolute_entry_or_none(
                    sap_result.get("AbsoluteEntry")
                )
                if not absolute_entry:
                    raise SAPDataError("SAP did not return AbsoluteEntry")

                attachment.sap_absolute_entry = absolute_entry
                attachment.sap_attachment_status = SAPAttachmentStatus.UPLOADED
                attachment.save(update_fields=[
                    "sap_absolute_entry", "sap_attachment_status"
                ])

                # Link attachment to the GRPO document
                sap_client.link_attachment_to_grpo(
                    doc_entry=grpo_posting.sap_doc_entry,
                    absolute_entry=absolute_entry
                )
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.save(update_fields=["sap_attachment_status"])

            logger.info(
                f"Attachment '{attachment.original_filename}' uploaded and linked "
                f"to GRPO DocEntry {grpo_posting.sap_doc_entry}"
            )

            return attachment

        except (SAPValidationError, SAPConnectionError, SAPDataError) as e:
            attachment.sap_attachment_status = SAPAttachmentStatus.FAILED
            attachment.sap_error_message = str(e)
            attachment.save(update_fields=[
                "sap_attachment_status", "sap_error_message"
            ])
            logger.error(
                f"Failed to upload attachment for GRPO {grpo_posting_id}: {e}"
            )
            # Return attachment with FAILED status — file is saved locally
            return attachment

    def retry_attachment_upload(
        self,
        attachment_id: int,
    ) -> GRPOAttachment:
        """
        Retry uploading a FAILED attachment to SAP.
        If upload succeeded but link failed, skips re-upload.
        """
        try:
            attachment = GRPOAttachment.objects.select_related(
                "grpo_posting"
            ).get(id=attachment_id)
        except GRPOAttachment.DoesNotExist:
            raise ValueError(f"Attachment {attachment_id} not found")

        if attachment.sap_attachment_status not in [
            SAPAttachmentStatus.PENDING,
            SAPAttachmentStatus.FAILED
        ]:
            raise ValueError(
                f"Attachment is already '{attachment.sap_attachment_status}'. "
                f"Only PENDING or FAILED attachments can be retried."
            )

        grpo_posting = attachment.grpo_posting
        if not grpo_posting.sap_doc_entry:
            raise ValueError("GRPO posting has no SAP DocEntry.")

        try:
            sap_client = SAPClient(company_code=self.company_code)

            # Check if GRPO already has an AttachmentEntry
            existing_abs_entry = sap_client.get_grpo_attachment_entry(
                grpo_posting.sap_doc_entry
            )
            existing_abs_entry = self._sap_absolute_entry_or_none(existing_abs_entry)

            if existing_abs_entry:
                # Add line to existing Attachments2 entry (avoids approval error)
                sap_client.add_line_to_existing_attachment(
                    absolute_entry=existing_abs_entry,
                    file_path=attachment.file.path,
                    filename=attachment.original_filename,
                    allow_metadata_fallback=True,
                )
                attachment.sap_absolute_entry = existing_abs_entry
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.sap_error_message = None
                attachment.save(update_fields=[
                    "sap_absolute_entry", "sap_attachment_status",
                    "sap_error_message"
                ])
            else:
                # No existing attachment — upload and link
                if attachment.sap_absolute_entry:
                    absolute_entry = attachment.sap_absolute_entry
                else:
                    sap_result = sap_client.upload_attachment(
                        file_path=attachment.file.path,
                        filename=attachment.original_filename,
                        allow_metadata_fallback=True,
                    )
                    absolute_entry = self._sap_absolute_entry_or_none(
                        sap_result.get("AbsoluteEntry")
                    )
                    if not absolute_entry:
                        raise SAPDataError("SAP did not return AbsoluteEntry")

                    attachment.sap_absolute_entry = absolute_entry
                    attachment.sap_attachment_status = SAPAttachmentStatus.UPLOADED
                    attachment.save(update_fields=[
                        "sap_absolute_entry", "sap_attachment_status"
                    ])

                sap_client.link_attachment_to_grpo(
                    doc_entry=grpo_posting.sap_doc_entry,
                    absolute_entry=absolute_entry
                )
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.sap_error_message = None
                attachment.save(update_fields=[
                    "sap_attachment_status", "sap_error_message"
                ])

            return attachment

        except (SAPValidationError, SAPConnectionError, SAPDataError) as e:
            attachment.sap_attachment_status = SAPAttachmentStatus.FAILED
            attachment.sap_error_message = str(e)
            attachment.save(update_fields=[
                "sap_attachment_status", "sap_error_message"
            ])
            return attachment
