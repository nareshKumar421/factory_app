from django.db import models


class DecisionAction(models.TextChoices):
    APPROVE = "APPROVE", "Approved"
    REJECT = "REJECT", "Rejected"
    WITHDRAW = "WITHDRAW", "Withdrawn"
