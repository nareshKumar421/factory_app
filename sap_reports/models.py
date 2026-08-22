"""
sap_reports/models.py

The catalogue of SAP saved queries this app can run.

SAP is the source of truth for a report's *SQL*: every row here mirrors one
``OUQR`` record in a company database and is refreshed by
``manage.py sync_sap_reports``. What SAP has no place to store -- a readable
name, a description, whether the report is fit to be shown, and what each
numbered prompt actually means -- lives here and survives every sync.

Results are never stored. Each run goes straight to HANA and only its shape
(row count, duration, who ran it) is kept, in ``SapReportRun``.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from company.models import Company

from .parameters import ParameterKind
from .sql import StatementKind, normalise_sql


class SapReportQuerySet(models.QuerySet):

    def for_company(self, company):
        return self.filter(company=company)

    def runnable(self):
        """Reports a user is allowed to see and run right now."""
        return self.filter(is_enabled=True, is_missing_in_sap=False, is_runnable=True)


class SapReport(models.Model):
    """One SAP Query Manager report, as offered by this app."""

    STATEMENT_KIND_CHOICES = [
        (StatementKind.SELECT, "Select"),
        (StatementKind.CALL, "Procedure call"),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="sap_reports",
        help_text="Company database this saved query belongs to.",
    )

    # --- mirrored from SAP (overwritten on every sync) ---------------------
    sap_internal_key = models.IntegerField(
        help_text="OUQR.IntrnalKey — the report's identity inside SAP.",
    )
    sap_name = models.CharField(
        max_length=200,
        help_text="OUQR.QName, exactly as the query is named in SAP.",
    )
    sap_category_id = models.IntegerField(
        help_text="OQCN.CategoryId of the SAP query category (e.g. Factory).",
    )
    sap_category_name = models.CharField(max_length=100)
    sql_text = models.TextField(help_text="OUQR.QString with line breaks normalised.")
    sql_hash = models.CharField(
        max_length=64,
        help_text="Fingerprint of sql_text; a change here means the query was edited in SAP.",
    )
    statement_kind = models.CharField(
        max_length=10,
        choices=STATEMENT_KIND_CHOICES,
        default=StatementKind.SELECT,
    )
    is_runnable = models.BooleanField(
        default=True,
        help_text="False when the saved query is not a single read-only statement.",
    )
    not_runnable_reason = models.CharField(max_length=255, blank=True)
    is_missing_in_sap = models.BooleanField(
        default=False,
        help_text="Set when a sync no longer finds this query in SAP; kept for its run history.",
    )
    sap_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="OUQR.UpdateDate — when the query was last edited inside SAP.",
    )

    # --- ours (never touched by a sync) ------------------------------------
    slug = models.SlugField(
        max_length=120,
        help_text="Stable URL name for this report within the company.",
    )
    display_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Friendlier name to show instead of the raw SAP query name.",
    )
    description = models.TextField(
        blank=True,
        help_text="What this report answers, for the people who run it.",
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text="Uncheck to hide the report from the module without deleting its history.",
    )
    sort_order = models.IntegerField(
        default=0,
        help_text="Lower sorts first; ties fall back to the report name.",
    )
    row_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Row ceiling for this report only; blank uses the module default.",
    )

    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SapReportQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sap_internal_key"],
                name="uniq_sap_report_per_company_key",
            ),
            models.UniqueConstraint(
                fields=["company", "slug"],
                name="uniq_sap_report_slug_per_company",
            ),
        ]
        ordering = ["company_id", "sort_order", "sap_name"]
        permissions = [
            ("can_view_sap_reports", "Can view and run SAP reports"),
            ("can_manage_sap_reports", "Can sync SAP reports and edit their setup"),
        ]
        verbose_name = "SAP report"
        verbose_name_plural = "SAP reports"

    def __str__(self):
        return f"{self.title} ({self.company.code})"

    @property
    def title(self) -> str:
        """What a user should see: the friendly name if one was given."""
        return self.display_name or self.sap_name

    @property
    def effective_row_limit(self) -> int:
        from .services.runner import DEFAULT_ROW_LIMIT

        return self.row_limit or DEFAULT_ROW_LIMIT

    def mark_run(self):
        self.last_run_at = timezone.now()
        self.save(update_fields=["last_run_at", "updated_at"])

    def normalised_sql(self) -> str:
        return normalise_sql(self.sql_text)


class SapReportParameter(models.Model):
    """
    One ``[%N]`` prompt of a report, described well enough to render a field.

    ``is_customised`` is the contract with the sync: once a person has corrected
    a label or type, later syncs leave this row alone.
    """

    report = models.ForeignKey(
        SapReport,
        on_delete=models.CASCADE,
        related_name="parameters",
    )
    position = models.PositiveSmallIntegerField(
        help_text="The N in SAP's [%N] placeholder.",
    )
    label = models.CharField(max_length=120)
    kind = models.CharField(
        max_length=20,
        choices=ParameterKind.CHOICES,
        default=ParameterKind.TEXT,
    )
    is_required = models.BooleanField(default=True)
    default_value = models.CharField(
        max_length=120,
        blank=True,
        help_text="Pre-filled value; used when the caller sends nothing for this prompt.",
    )
    blank_value = models.CharField(
        max_length=64,
        blank=True,
        help_text="What to bind when an optional prompt is left empty (usually blank).",
    )
    help_text = models.CharField(max_length=200, blank=True)
    is_quoted = models.BooleanField(
        default=True,
        help_text="True when SAP wrote the placeholder inside quotes, i.e. bind it as text.",
    )
    occurrences = models.PositiveSmallIntegerField(
        default=1,
        help_text="How many times this prompt appears in the SQL.",
    )
    is_customised = models.BooleanField(
        default=False,
        help_text="Set when a person edited this parameter; protects it from later syncs.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "position"],
                name="uniq_sap_report_parameter_position",
            ),
        ]
        ordering = ["report_id", "position"]
        verbose_name = "SAP report parameter"
        verbose_name_plural = "SAP report parameters"

    def __str__(self):
        return f"{self.report.title} [%{self.position}] {self.label}"

    @property
    def has_lookup(self) -> bool:
        """Whether the API can offer a picklist for this parameter."""
        return self.kind in ParameterKind.LOOKUP_KINDS


class SapReportRun(models.Model):
    """
    An audit trail of report executions -- who ran what, with which filters.

    Report rows themselves are never stored: they are read live from SAP and
    streamed to the caller. This table exists so a slow or heavily-used report
    can be found, and so a company can see who pulled which numbers.
    """

    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        ERROR = "ERROR", "Error"

    report = models.ForeignKey(
        SapReport,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="sap_report_runs",
    )
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sap_report_runs",
    )
    parameters = models.JSONField(
        default=dict,
        blank=True,
        help_text="The values the report was run with, keyed by prompt position.",
    )
    status = models.CharField(max_length=10, choices=Status.choices)
    row_count = models.PositiveIntegerField(default=0)
    was_truncated = models.BooleanField(
        default=False,
        help_text="True when the row ceiling cut the result short.",
    )
    duration_ms = models.PositiveIntegerField(default=0)
    export_format = models.CharField(
        max_length=10,
        blank=True,
        help_text="Blank for an on-screen run; csv/xlsx for a download.",
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["report", "-created_at"]),
            models.Index(fields=["company", "-created_at"]),
        ]
        verbose_name = "SAP report run"
        verbose_name_plural = "SAP report runs"

    def __str__(self):
        return f"{self.report_id} by {self.run_by_id} at {self.created_at:%Y-%m-%d %H:%M}"
