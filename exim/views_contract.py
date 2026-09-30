"""Oil contract endpoints: the register, one purchase order, and its terms.

Thin on purpose: check the right, work inside the caller's company, call one
function in ``exim.services_contract``. The register and a PO are read live
from SAP and the gate, so they send plain numbers (a read-out), not stored
decimals.
"""

from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from . import services_contract
from .models_contract import DeliveryTerms
from .permissions import Rights
from .views_tank import _Base, _company

_VIEW = (Rights.CONTRACT_VIEW, Rights.LANDED_COST_VIEW)


class ContractTermsSerializer(serializers.Serializer):
    delivery_terms = serializers.ChoiceField(choices=DeliveryTerms.choices, allow_blank=True)
    freight_per_mt = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False,
                                              allow_null=True, default=0)
    brokerage_per_mt = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False,
                                                allow_null=True, default=0)
    note = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")


class ContractListAPI(_Base):
    """GET [?year=2026 | ?open=1] : the oil contracts of a financial year (April
    to March, by PO date; this year's when left out), or every one still open."""

    rights = {"GET": _VIEW}

    def get(self, request):
        params = request.query_params
        open_only = params.get("open") in ("1", "true")
        year = params.get("year")
        if year is not None and not (year.isdigit() and 2000 <= int(year) <= 2100):
            raise ValidationError({"year": "Pass the year the financial year starts in, e.g. 2026."})
        return Response(services_contract.contracts(
            _company(request), year=int(year) if year else None, open_only=open_only,
        ))


class ContractDetailAPI(_Base):
    """GET : one purchase order - each oil line with every truck against it."""

    rights = {"GET": _VIEW}

    def get(self, request, po_number):
        return Response(services_contract.contract(_company(request), po_number))


class ContractTermsAPI(_Base):
    """PUT {delivery_terms, freight_per_mt, brokerage_per_mt, note} : how a PO is
    delivered, and the freight and brokerage its landed cost adds."""

    rights = {"PUT": (Rights.CONTRACT_CHANGE,)}

    def put(self, request, po_number):
        serializer = ContractTermsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        terms = services_contract.set_terms(
            _company(request), po_number, user=request.user,
            delivery_terms=data["delivery_terms"],
            freight_per_mt=data.get("freight_per_mt") or 0,
            brokerage_per_mt=data.get("brokerage_per_mt") or 0,
            note=data.get("note", ""),
        )
        return Response(services_contract.terms_dict(terms))
