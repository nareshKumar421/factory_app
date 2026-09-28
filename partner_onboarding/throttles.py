"""
Rate limits for the public (no-login) endpoints.

The registration forms are open to anyone, so a script could fill the queue or
the disk. These cap each client address. The rates live on the classes rather
than in ``REST_FRAMEWORK`` (which configures no throttling for the rest of the
API), so nothing else changes behaviour.

The counters use Django's default cache. With no ``CACHES`` configured that is
per-process memory, so each worker counts on its own: the effective ceiling is
the rate times the number of workers. That still stops a flood; point
``CACHES`` at Redis to make it exact.

Which address: behind the nginx that fronts this server every request arrives
from the proxy, so ``REMOTE_ADDR`` is the same for everyone. DRF's default
then reads the whole ``X-Forwarded-For`` header, which the client can prefix
with anything to get a fresh bucket per request. The right-most entry is the
one our own proxy appended — the address it actually saw — so that is the one
counted, and ``REMOTE_ADDR`` only when there is no header at all.
"""

from rest_framework.throttling import AnonRateThrottle

# Holding any of these marks the sender as staff working the queue, who open
# the public form from it (the queue page links there) and may send many.
STAFF_RIGHTS = (
    "partner_onboarding.can_view_customer_registrations",
    "partner_onboarding.can_verify_customer_registrations",
    "partner_onboarding.can_approve_customer_registrations",
    "partner_onboarding.can_view_vendor_registrations",
    "partner_onboarding.can_verify_vendor_registrations",
    "partner_onboarding.can_approve_vendor_registrations",
)


def staff_sender(request):
    """The logged-in partner-onboarding staff member behind a public request, or None.

    The public views authenticate nobody, so that a stale token in a browser
    cannot turn a submission into a 401. The token is therefore read here,
    quietly: a missing, expired or forged one simply means a member of the
    public, who stays throttled.
    """
    if not request.META.get("HTTP_AUTHORIZATION", "").startswith("Bearer "):
        return None
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication

        found = JWTAuthentication().authenticate(request)
    except Exception:
        return None
    if not found:
        return None
    user = found[0]
    if not getattr(user, "is_active", False):
        return None
    return user if any(user.has_perm(right) for right in STAFF_RIGHTS) else None


class _PublicThrottle(AnonRateThrottle):
    def get_ident(self, request):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
        return request.META.get("REMOTE_ADDR") or "unknown"


class PublicSubmitThrottle(_PublicThrottle):
    """Registrations sent, per client address. Every attempt counts, valid or not.

    Staff working the queue (:func:`staff_sender`) are not counted: they
    register partners through this same form, often many in a day, and an
    office behind one address would otherwise lock itself out.
    """

    scope = "partner_onboarding_submit"
    rate = "10/hour"

    def allow_request(self, request, view):
        if staff_sender(request) is not None:
            return True
        return super().allow_request(request, view)


class PublicReadThrottle(_PublicThrottle):
    """The form's own reads (company list, states)."""

    scope = "partner_onboarding_read"
    rate = "120/hour"
