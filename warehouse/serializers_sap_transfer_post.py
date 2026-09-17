"""Serializer for posting a transfer against a SAP-raised transfer request."""
from rest_framework import serializers


class SapTransferPostSerializer(serializers.Serializer):
    """``{"quantities": {"<WTQ1.LineNum>": "<qty>"}}``.

    Quantities arrive as strings and stay strings here: the service parses them
    with ``Decimal`` so a value like 143.846 is not rounded on its way through a
    float. Line numbers are validated against the live request, not here, since
    only SAP knows which of its lines are still open.
    """

    quantities = serializers.DictField(
        child=serializers.CharField(), allow_empty=False
    )
