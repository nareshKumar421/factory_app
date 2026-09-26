from rest_framework import serializers

from .models import SapBudgetChange


class JournalEntryFilterSerializer(serializers.Serializer):
    """Query parameters of the journal-entry list (all optional)."""

    trans_id = serializers.IntegerField(required=False, min_value=1)
    number = serializers.IntegerField(required=False, min_value=1)
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100)
    trans_type = serializers.CharField(required=False, allow_blank=True, max_length=20)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)

    def validate(self, attrs):
        if attrs.get("date_from") and attrs.get("date_to") and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "The end date is before the start date."})
        return attrs


class LedgerFilterSerializer(serializers.Serializer):
    account = serializers.CharField(max_length=50)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=1000, default=200)

    def validate(self, attrs):
        if attrs.get("date_from") and attrs.get("date_to") and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "The end date is before the start date."})
        return attrs


class BudgetLineSerializer(serializers.Serializer):
    month = serializers.DateField()
    fixed_amount = serializers.DecimalField(max_digits=19, decimal_places=2, min_value=0)
    variable_amount = serializers.DecimalField(max_digits=19, decimal_places=2, min_value=0)
    sub_budget = serializers.CharField(required=False, allow_blank=True, max_length=50, default="")


class BudgetWriteSerializer(serializers.Serializer):
    """A budget as the screen sends it: a head (dimension 3), an optional
    sub-budget (dimension 4) and one line per month."""

    budget = serializers.CharField(max_length=50)
    sub_budget = serializers.CharField(required=False, allow_blank=True, max_length=50, default="")
    lines = BudgetLineSerializer(many=True)

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("Add at least one month line.")
        months = [line["month"] for line in lines]
        if len(months) != len(set(months)):
            raise serializers.ValidationError("Each month may appear only once.")
        return lines

    def to_sap(self) -> dict:
        """The ``BUDGET`` payload, field for field as SAP Portal sent it."""
        data = self.validated_data
        return {
            "U_BUDGET": data["budget"].strip(),
            "U_SUB_BUDGET": (data.get("sub_budget") or "").strip() or None,
            "BUDGET1Collection": [
                {
                    "U_MONTH": line["month"].isoformat(),
                    "U_FIXED_AMOUNT": float(line["fixed_amount"]),
                    "U_V_AMOUNT": float(line["variable_amount"]),
                    "U_SUB_BUDGET": (line.get("sub_budget") or "").strip() or None,
                }
                for line in data["lines"]
            ],
        }


def budget_from_sap(document: dict) -> dict:
    """A SAP ``BUDGET`` entity in the app's own field names."""
    return {
        "doc_entry": document.get("DocEntry"),
        "doc_num": document.get("DocNum"),
        "budget": document.get("U_BUDGET") or "",
        "sub_budget": document.get("U_SUB_BUDGET") or "",
        "created_at": (document.get("CreateDate") or "")[:10] or None,
        "lines": [
            {
                "line_id": line.get("LineId"),
                "month": (line.get("U_MONTH") or "")[:10] or None,
                "fixed_amount": float(line.get("U_FIXED_AMOUNT") or 0),
                "variable_amount": float(line.get("U_V_AMOUNT") or 0),
                "sub_budget": line.get("U_SUB_BUDGET") or "",
            }
            for line in document.get("BUDGET1Collection") or []
        ],
    }


class SapBudgetChangeSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = SapBudgetChange
        fields = [
            "id", "action", "action_label", "doc_entry", "budget_code", "sub_budget_code",
            "line_count", "payload", "changed_by", "created_at",
        ]
        read_only_fields = fields

    def get_changed_by(self, row):
        user = row.created_by
        return (getattr(user, "full_name", "") or getattr(user, "email", "")) if user else ""
