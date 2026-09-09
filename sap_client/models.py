"""Which SAP B1 account each app user is, per company.

SAP accepts an approval decision from exactly one account — the authorizer its
approval template names on the request's current stage — so the app has to know
whether the person clicking *is* that authorizer. Without this link the app can
only hold a pool of shared credentials and sign with whichever one fits, which
makes the decision anonymous: SAP records the authorizer, our audit records the
clicker, and nothing guarantees they are the same human.

The mapping is per **company**, not global: ``USER37`` is the same person in
Oil, Mart and Beverages but a separate SAP account in each, with its own
password, and one person can hold different codes in different companies.

Passwords are deliberately NOT stored here. They stay in the
``SAP_APPROVER_CREDENTIALS`` env map, keyed by the same SAP user code, so this
table can be edited by an administrator through the app while the secrets stay
out of the database.
"""
from django.conf import settings
from django.db import models

from gate_core.models import BaseModel


class SapApproverIdentity(BaseModel):
    """One app user ↔ one SAP B1 user account, within one company."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sap_identities",
    )
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="sap_approver_identities",
    )
    # OUSR.USER_CODE, e.g. 'USER37'. Upper-cased on save so a lookup against
    # what HANA reports can be a plain equality test.
    sap_user_code = models.CharField(max_length=50)
    # OUSR.U_NAME at the time of linking — display only, so the admin page can
    # label a row without a HANA round trip. Never used for matching.
    sap_user_name = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        db_table = "sap_approver_identity"
        ordering = ["company__code", "sap_user_code"]
        constraints = [
            # A person is one SAP account per company...
            models.UniqueConstraint(
                fields=["user", "company"], name="unique_sap_identity_per_user_company"
            ),
            # ...and a SAP account belongs to one person, or two people could
            # both act as the same authorizer and the audit trail would not say
            # which of them did.
            models.UniqueConstraint(
                fields=["company", "sap_user_code"],
                name="unique_sap_identity_per_company_code",
            ),
        ]
        default_permissions = ()
        permissions = [
            ("can_manage_sap_identities", "Can map app users to SAP user accounts"),
        ]

    def save(self, *args, **kwargs):
        self.sap_user_code = (self.sap_user_code or "").strip().upper()
        self.sap_user_name = (self.sap_user_name or "").strip()
        super().save(*args, **kwargs)

    @property
    def password_configured(self) -> bool:
        """Whether this SAP account's password is in the env credential map.

        The link alone does not let someone approve: the app still has to be
        able to authenticate as them.
        """
        credentials = settings.SAP_APPROVER_CREDENTIALS.get(self.company.code) or {}
        return bool(credentials.get(self.sap_user_code))

    @classmethod
    def code_for(cls, user, company) -> str | None:
        """The SAP user code ``user`` acts as in ``company``, if any."""
        row = (
            cls.objects.filter(user=user, company=company, is_active=True)
            .only("sap_user_code")
            .first()
        )
        return row.sap_user_code if row else None

    def __str__(self):
        return f"{self.user} = {self.sap_user_code} @ {self.company.code}"
