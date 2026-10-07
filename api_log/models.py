"""Every call made to the API: who, what, when, and how it went.

One row per request under ``/api/``, written by :mod:`api_log.middleware` once
the response is ready. It answers "what do people actually use" (group by
``app_label`` or ``route``) and "what happened to that call" (find it by user,
path and time).

Bodies are kept only where they tell a story: a write (POST, PUT, PATCH,
DELETE) keeps what was sent and what came back, and any call that failed (a 4xx
or 5xx) keeps its answer. A plain successful read keeps neither, or the
dashboards' polling alone would fill the table. Secrets are blanked out before
anything is stored, and bodies are cut short; see the middleware.

Rows older than ``API_LOG_RETENTION_DAYS`` are deleted by ``manage.py
prune_api_log``, run nightly by a systemd timer (see ``api_log/deploy/``).
"""

from django.conf import settings
from django.db import models


class ApiCall(models.Model):
    started_at = models.DateTimeField()
    #: Blank when nobody was signed in, or the token was refused.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_calls",
    )
    #: The ``Company-Code`` header the call was made under.
    company_code = models.CharField(max_length=32, blank=True)

    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    query_string = models.TextField(blank=True)
    #: The URL pattern that answered, e.g. ``api/v1/grpo/<int:pk>/post/``: the
    #: same for every record, so calls of one endpoint group together.
    route = models.CharField(max_length=300, blank=True)
    #: The view that answered, e.g. ``grpo.views.GRPOPostView``.
    view = models.CharField(max_length=255, blank=True)
    #: The Django app that view belongs to: the module, for "what is used most".
    app_label = models.CharField(max_length=60, blank=True)

    status_code = models.PositiveSmallIntegerField()
    #: Until the response was ready; a streamed download keeps going after.
    duration_ms = models.PositiveIntegerField()

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    request_bytes = models.PositiveIntegerField(null=True, blank=True)
    response_bytes = models.PositiveIntegerField(null=True, blank=True)
    request_body = models.TextField(blank=True)
    response_body = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["-started_at"], name="api_call_time_idx"),
            models.Index(fields=["user", "-started_at"], name="api_call_user_idx"),
            models.Index(fields=["app_label", "-started_at"], name="api_call_app_idx"),
        ]

    def __str__(self):
        return f"{self.method} {self.path} {self.status_code}"
