"""Routes under ``/api/v1/partner-onboarding/``.

``public/…`` needs no login (the registration forms); everything else is the
approvals queue, once for customers and once for vendors.
"""

from django.urls import path

from .views import (
    RegistrationApproveAPI,
    RegistrationAttachmentAPI,
    RegistrationDetailAPI,
    RegistrationListAPI,
    RegistrationRejectAPI,
    RegistrationVerifyAPI,
)
from .views_public import PublicCompaniesAPI, PublicStatesAPI, PublicSubmitAPI

urlpatterns = [
    path("public/companies/", PublicCompaniesAPI.as_view(), name="partner-onboarding-public-companies"),
    path("public/states/", PublicStatesAPI.as_view(), name="partner-onboarding-public-states"),
    path(
        "public/customers/",
        PublicSubmitAPI.as_view(family="customer"),
        name="partner-onboarding-public-customers",
    ),
    path(
        "public/vendors/",
        PublicSubmitAPI.as_view(family="vendor"),
        name="partner-onboarding-public-vendors",
    ),
]

for _family, _plural in (("customer", "customers"), ("vendor", "vendors")):
    urlpatterns += [
        path(f"{_plural}/", RegistrationListAPI.as_view(family=_family), name=f"partner-onboarding-{_plural}"),
        path(
            f"{_plural}/<int:pk>/",
            RegistrationDetailAPI.as_view(family=_family),
            name=f"partner-onboarding-{_family}-detail",
        ),
        path(
            f"{_plural}/<int:pk>/verify/",
            RegistrationVerifyAPI.as_view(family=_family),
            name=f"partner-onboarding-{_family}-verify",
        ),
        path(
            f"{_plural}/<int:pk>/reject/",
            RegistrationRejectAPI.as_view(family=_family),
            name=f"partner-onboarding-{_family}-reject",
        ),
        path(
            f"{_plural}/<int:pk>/approve/",
            RegistrationApproveAPI.as_view(family=_family),
            name=f"partner-onboarding-{_family}-approve",
        ),
        path(
            f"{_plural}/<int:pk>/attachments/<int:attachment_id>/",
            RegistrationAttachmentAPI.as_view(family=_family),
            name=f"partner-onboarding-{_family}-attachment",
        ),
    ]
