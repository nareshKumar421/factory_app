from django.db import models


class DecisionAction(models.TextChoices):
    APPROVE = "APPROVE", "Approved"
    REJECT = "REJECT", "Rejected"
    WITHDRAW = "WITHDRAW", "Withdrawn"


class RejectionCategory(models.TextChoices):
    """What kind of entry was rejected — the accounts desk's own heads.

    SAP has nothing like it: the approval template names the approver, not the
    expense, and most drafts carry no budget head. So the approver picks one
    when rejecting, and the rejection history groups by it. A rejection taken
    in the SAP client has none and shows its GL account instead.
    """

    CASH_VOUCHER = "CASH_VOUCHER", "Cash Voucher"
    ELECTRICITY = "ELECTRICITY", "Electricity"
    FUEL = "FUEL", "Fuel"
    IMPREST = "IMPREST", "Imprest"
    RENT = "RENT", "Rent"
    REPAIRS = "REPAIRS", "R&M"
    SERVICE = "SERVICE", "Service"
    SUBSCRIPTION = "SUBSCRIPTION", "Subscription"
    TRANSPORT = "TRANSPORT", "Transport"
    UTILITY = "UTILITY", "Utility"
    OTHER = "OTHER", "Other"
