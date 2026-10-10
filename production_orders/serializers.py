from rest_framework import serializers

from .constants import LINE_CODES
from .models import STEPS, ProductionOrderEntry, ProductionOrderEntryLine, Step
from .permissions import can_take


class _StepInput(serializers.Serializer):
    """Every step's form: its own fields, then save — and post, when ``post``."""

    post = serializers.BooleanField(default=False)


class PlanInputSerializer(_StepInput):
    item_code = serializers.CharField(max_length=50)
    boxes = serializers.IntegerField(min_value=0, default=0)
    loose_pieces = serializers.IntegerField(min_value=0, default=0)
    posting_date = serializers.DateField(required=False, allow_null=True)
    remarks = serializers.CharField(max_length=200, required=False, allow_blank=True)


class BatchPickSerializer(serializers.Serializer):
    batch_number = serializers.CharField(max_length=36)
    quantity = serializers.DecimalField(max_digits=19, decimal_places=6, min_value=0)


class LineBatchesSerializer(serializers.Serializer):
    line_id = serializers.IntegerField()
    batches = BatchPickSerializer(many=True)


class IssueInputSerializer(_StepInput):
    issue_date = serializers.DateField(required=False, allow_null=True)
    variety = serializers.CharField(max_length=8, required=False, allow_blank=True)
    lines = LineBatchesSerializer(many=True, required=False)


class ReceiptInputSerializer(_StepInput):
    receipt_date = serializers.DateField(required=False, allow_null=True)
    line_code = serializers.ChoiceField(choices=[("", "")] + list(LINE_CODES.items()), required=False)
    oil_code = serializers.CharField(max_length=10, required=False, allow_blank=True)
    mfg_date = serializers.DateField(required=False, allow_null=True)
    batch_sequence = serializers.IntegerField(min_value=1, max_value=99, required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)


class CloseInputSerializer(_StepInput):
    close_date = serializers.DateField(required=False, allow_null=True)


class ReleaseInputSerializer(_StepInput):
    pass


STEP_INPUTS = {
    Step.PLAN: PlanInputSerializer,
    Step.RELEASE: ReleaseInputSerializer,
    Step.ISSUE: IssueInputSerializer,
    Step.RECEIPT: ReceiptInputSerializer,
    Step.CLOSE: CloseInputSerializer,
}


def _posting(posting):
    if posting is None:
        return None
    return {
        "id": posting.pk,
        "status": posting.status,
        "status_label": posting.get_status_display(),
        "attempts": posting.attempts,
        "last_error": posting.last_error,
        "updated_at": posting.updated_at,
    }


def _name(user):
    if user is None:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class EntryLineSerializer(serializers.ModelSerializer):
    batches = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOrderEntryLine
        fields = [
            "id", "position", "item_code", "item_name", "item_type", "issue_method",
            "base_quantity", "planned_quantity", "warehouse", "uom", "batch_managed",
            "sap_line_num", "batches",
        ]

    def get_batches(self, line):
        return [{"batch_number": b.batch_number, "quantity": str(b.quantity)} for b in line.batches.all()]


class EntryListSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display")
    next_step = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOrderEntry
        fields = [
            "id", "entry_no", "kind", "status", "status_label", "next_step", "item_code", "item_name",
            "boxes", "loose_pieces", "quantity", "pieces_per_box", "uom", "warehouse",
            "batch_number", "posting_date", "sap_order_num", "sap_issue_num", "sap_receipt_num",
            "created_at", "created_by_name",
        ]

    def get_created_by_name(self, entry):
        return _name(entry.created_by)

    def get_next_step(self, entry):
        return entry.next_step or None


class EntryDetailSerializer(EntryListSerializer):
    lines = EntryLineSerializer(many=True, read_only=True)
    steps = serializers.SerializerMethodField()
    changes = serializers.SerializerMethodField()
    line_label = serializers.SerializerMethodField()

    class Meta(EntryListSerializer.Meta):
        fields = EntryListSerializer.Meta.fields + [
            "litres_per_piece", "bom_quantity", "remarks", "variety", "issue_date", "receipt_date",
            "line_code", "line_label", "oil_code", "batch_sequence", "mfg_date", "expiry_date",
            "close_date", "sap_order_entry", "sap_issue_entry", "sap_receipt_entry", "lines", "steps",
            "changes", "updated_at",
        ]

    def get_line_label(self, entry):
        return LINE_CODES.get(entry.line_code, "")

    def get_changes(self, entry):
        """The latest posting of each change to the order in SAP (REPLAN, UNRELEASE)."""
        postings = self.context.get("change_postings") or {}
        return {change.lower(): _posting(postings.get(change)) for change in ("REPLAN", "UNRELEASE")}

    def get_steps(self, entry):
        """Each step: done or not, who took it, its SAP document, its latest posting."""
        postings = self.context.get("postings") or {}
        user = self.context["request"].user
        people = {
            Step.PLAN: (entry.planned_by, entry.planned_at, entry.sap_order_num),
            Step.RELEASE: (entry.released_by, entry.released_at, entry.sap_order_num),
            Step.ISSUE: (entry.issued_by, entry.issued_at, entry.sap_issue_num),
            Step.RECEIPT: (entry.received_by, entry.received_at, entry.sap_receipt_num),
            Step.CLOSE: (entry.closed_by, entry.closed_at, entry.sap_order_num),
        }
        rows = []
        for step in STEPS:
            by, at, doc_num = people[step]
            done = entry.step_done(step)
            posting = postings.get(step)
            rows.append(
                {
                    "step": step,
                    "label": Step(step).label,
                    "done": done,
                    "is_next": entry.next_step == step,
                    "by": _name(by),
                    "at": at,
                    "sap_doc_num": doc_num if done else None,
                    "can_take": can_take(user, step),
                    "posting": _posting(posting),
                }
            )
        return rows
