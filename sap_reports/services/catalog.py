"""
sap_reports/services/catalog.py

Keeping our report catalogue in step with SAP's Query Manager.

A sync reads the saved queries of one SAP query category and mirrors them into
``SapReport`` rows: new queries appear, edited SQL is refreshed, and a query that
has been deleted in SAP is flagged rather than dropped so its run history stays
intact. Everything a person added on our side -- the friendly name, the
description, corrected parameter labels -- is left alone.

This is what makes the module hands-off: a new report authored in SAP shows up in
the app after a sync, with no code change.
"""

import logging
from typing import Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from sap_client.context import CompanyContext

from ..hana_reader import HanaSapReportReader
from ..models import SapReport, SapReportParameter
from ..parameters import infer_parameters
from ..sql import detect_statement_kind, is_runnable, normalise_sql, sql_hash

logger = logging.getLogger(__name__)

# The SAP query category these reports live in. Everything the factory team
# maintains is filed under "Factory"; other categories can be synced by name.
DEFAULT_CATEGORY = "Factory"


class SapReportCatalogService:
    """Discovers and refreshes the SAP reports available to one company."""

    def __init__(self, company):
        self.company = company
        self.context = CompanyContext(company.code)
        self.reader = HanaSapReportReader(self.context)

    # ------------------------------------------------------------------
    # Reading SAP's own catalogue
    # ------------------------------------------------------------------

    def list_categories(self) -> List[Dict]:
        """SAP's query categories, so an admin can see what else could be synced."""
        return self.reader.list_categories()

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(
        self,
        category_name: Optional[str] = DEFAULT_CATEGORY,
        *,
        dry_run: bool = False,
    ) -> Dict:
        """
        Mirrors one SAP query category into ``SapReport`` rows.

        Returns a summary of what changed, which is what both the management
        command and the sync endpoint report back.
        """
        saved_queries = self.reader.list_saved_queries(category_name)

        summary = {
            "company": self.company.code,
            "category": category_name or "(all)",
            "found_in_sap": len(saved_queries),
            "created": [],
            "updated": [],
            "unchanged": [],
            "not_runnable": [],
            "missing_in_sap": [],
            "dry_run": dry_run,
        }

        if dry_run:
            for saved_query in saved_queries:
                runnable, reason = is_runnable(saved_query["sql_text"])
                bucket = "created" if not self._existing(saved_query) else "updated"
                summary[bucket].append(saved_query["sap_name"])
                if not runnable:
                    summary["not_runnable"].append(f"{saved_query['sap_name']}: {reason}")
            return summary

        with transaction.atomic():
            for saved_query in saved_queries:
                report, outcome = self._upsert(saved_query)
                summary[outcome].append(report.sap_name)
                if not report.is_runnable:
                    summary["not_runnable"].append(
                        f"{report.sap_name}: {report.not_runnable_reason}"
                    )

            summary["missing_in_sap"] = self._flag_missing(
                category_name=category_name,
                seen_keys=[query["sap_internal_key"] for query in saved_queries],
            )

        return summary

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _existing(self, saved_query: Dict) -> Optional[SapReport]:
        return SapReport.objects.filter(
            company=self.company,
            sap_internal_key=saved_query["sap_internal_key"],
        ).first()

    def _upsert(self, saved_query: Dict):
        """Creates or refreshes one report; returns ``(report, "created"|"updated"|"unchanged")``."""
        sql_text = normalise_sql(saved_query["sql_text"])
        new_hash = sql_hash(sql_text)
        runnable, reason = is_runnable(sql_text)

        report = self._existing(saved_query)
        is_new = report is None

        if is_new:
            report = SapReport(
                company=self.company,
                sap_internal_key=saved_query["sap_internal_key"],
                slug=self._unique_slug(saved_query["sap_name"]),
            )

        sql_changed = is_new or report.sql_hash != new_hash
        name_changed = not is_new and report.sap_name != saved_query["sap_name"]

        report.sap_name = saved_query["sap_name"]
        report.sap_category_id = saved_query["sap_category_id"]
        report.sap_category_name = saved_query["sap_category_name"]
        report.sql_text = sql_text
        report.sql_hash = new_hash
        report.statement_kind = detect_statement_kind(sql_text)
        report.is_runnable = runnable
        report.not_runnable_reason = "" if runnable else reason[:255]
        report.sap_changed_at = saved_query.get("sap_changed_at")
        report.is_missing_in_sap = False
        report.last_synced_at = timezone.now()
        report.save()

        # Prompts only move when the SQL does, so a stable report leaves its
        # parameter rows -- and any hand-corrected labels -- untouched.
        if sql_changed:
            self._sync_parameters(report)

        if is_new:
            return report, "created"
        return report, "updated" if (sql_changed or name_changed) else "unchanged"

    def _sync_parameters(self, report: SapReport) -> None:
        """
        Re-reads the report's prompts, preserving anything a person corrected.

        A customised parameter keeps its label, type and requiredness; only the
        mechanical facts (how the placeholder is written, how often it appears)
        are refreshed, because those come from the SQL and nowhere else.
        """
        inferred = infer_parameters(report.sql_text)
        existing = {parameter.position: parameter for parameter in report.parameters.all()}

        for guess in inferred:
            parameter = existing.pop(guess.position, None)

            if parameter is None:
                SapReportParameter.objects.create(
                    report=report,
                    position=guess.position,
                    label=guess.label,
                    kind=guess.kind,
                    is_required=guess.is_required,
                    help_text=guess.help_text,
                    is_quoted=guess.is_quoted,
                    occurrences=guess.occurrences,
                )
                continue

            parameter.is_quoted = guess.is_quoted
            parameter.occurrences = guess.occurrences
            if not parameter.is_customised:
                parameter.label = guess.label
                parameter.kind = guess.kind
                parameter.is_required = guess.is_required
                parameter.help_text = guess.help_text
            parameter.save()

        # Prompts the query no longer has.
        for orphan in existing.values():
            orphan.delete()

    def _flag_missing(self, *, category_name: Optional[str], seen_keys: List[int]) -> List[str]:
        """
        Marks reports SAP no longer has, without touching other categories.

        Deliberately a flag and not a delete: the run history is the only record
        of who pulled which numbers, and a query deleted in SAP by accident is
        common enough that losing our side of it would be worse.
        """
        stale = SapReport.objects.filter(company=self.company).exclude(
            sap_internal_key__in=seen_keys
        )
        if category_name:
            stale = stale.filter(sap_category_name__iexact=category_name)

        names = list(stale.values_list("sap_name", flat=True))
        stale.update(is_missing_in_sap=True, updated_at=timezone.now())
        if names:
            logger.info(
                "SAP reports no longer in SAP for %s: %s", self.company.code, ", ".join(names)
            )
        return names

    def _unique_slug(self, sap_name: str) -> str:
        """A URL-safe name for the report, unique within the company."""
        base = slugify(sap_name) or "report"
        base = base[:110]
        slug = base
        suffix = 2
        while SapReport.objects.filter(company=self.company, slug=slug).exists():
            slug = f"{base}-{suffix}"
            suffix += 1
        return slug
