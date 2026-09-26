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


class _PublicThrottle(AnonRateThrottle):
    def get_ident(self, request):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
        return request.META.get("REMOTE_ADDR") or "unknown"


class PublicSubmitThrottle(_PublicThrottle):
    """Registrations sent, per client address. Every attempt counts, valid or not."""

    scope = "partner_onboarding_submit"
    rate = "10/hour"


class PublicReadThrottle(_PublicThrottle):
    """The form's own reads (company list, states)."""

    scope = "partner_onboarding_read"
    rate = "120/hour"
