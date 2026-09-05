"""
Serializers for the ownership chart.

Reading returns the whole chart in one nested payload — it is one screen, and
seven departments' worth of rows is small. Writing takes the same shape back:
see :mod:`org_chart.services` for why the page saves the chart whole rather than
row by row.
"""

from rest_framework import serializers

from .models import OrgDepartment, OrgFunction

#: Longest a single person / collective name may be.
MAX_PERSON_NAME = 120


class OrgFunctionSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrgFunction
        fields = ["id", "name", "owners", "level_1", "level_2", "sort_order"]


class OrgDepartmentSerializer(serializers.ModelSerializer):
    functions = OrgFunctionSerializer(many=True, read_only=True)

    class Meta:
        model = OrgDepartment
        fields = ["id", "name", "sort_order", "functions"]


def _clean_names(values):
    """Trim, drop blanks, and drop repeats (case-insensitively) keeping order."""
    cleaned, seen = [], set()
    for value in values:
        name = " ".join(str(value).split())
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


class PeopleListField(serializers.ListField):
    child = serializers.CharField(max_length=MAX_PERSON_NAME, allow_blank=True)

    def to_internal_value(self, data):
        return _clean_names(super().to_internal_value(data))


class FunctionInputSerializer(serializers.Serializer):
    """One row being saved. ``id`` present = update that row, absent = create."""

    id = serializers.IntegerField(required=False, allow_null=True)
    name = serializers.CharField(
        max_length=150, required=False, allow_blank=True, default=""
    )
    owners = PeopleListField(required=False, default=list)
    level_1 = PeopleListField(required=False, default=list)
    level_2 = PeopleListField(required=False, default=list)

    def validate_name(self, value):
        return " ".join(value.split())


class DepartmentInputSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    name = serializers.CharField(max_length=120)
    functions = FunctionInputSerializer(many=True, required=False, default=list)

    def validate_name(self, value):
        name = " ".join(value.split())
        if not name:
            raise serializers.ValidationError("A department needs a name.")
        return name

    def validate(self, attrs):
        names = [function["name"].casefold() for function in attrs.get("functions", [])]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            label = ", ".join(sorted(name or "(no sub-department)" for name in duplicates))
            raise serializers.ValidationError(
                f"'{attrs['name']}' lists the same sub-department twice: {label}."
            )
        return attrs


class ChartSaveSerializer(serializers.Serializer):
    departments = DepartmentInputSerializer(many=True)

    def validate_departments(self, value):
        names = [department["name"].casefold() for department in value]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            label = ", ".join(sorted(duplicates))
            raise serializers.ValidationError(
                f"Two departments cannot share a name: {label}."
            )
        return value
