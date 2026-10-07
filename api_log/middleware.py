"""Writes an :class:`~api_log.models.ApiCall` for every call under ``/api/``.

It sits last in ``MIDDLEWARE``, so the time it measures is the view's own. The
caller is read once the view has run: DRF checks the JWT inside the view and
hands the user back to the underlying request, so only then is
``request.user`` the person who called.

A write's body has to be read before the view runs, because DRF consumes the
request stream and Django will not hand it out a second time. Only JSON and
plain form bodies are read up front, and only small ones; an upload is recorded
from what DRF already parsed (its fields and file names, never the files).

Recording never breaks a call: whatever goes wrong here is logged, and the
response goes out as it was.
"""

import ipaddress
import json
import logging
import time

from django.conf import settings
from django.db import transaction
from django.http import QueryDict
from django.utils import timezone

from .models import ApiCall

logger = logging.getLogger(__name__)

#: Only calls under here are recorded: not the admin, media or static files.
RECORDED_PREFIX = "/api/"
#: Monitoring pings: nobody signed in, nothing used, every minute.
SKIPPED_PREFIXES = ("/api/v1/health/",)
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
#: Characters of a body (or query string) that are kept.
BODY_LIMIT = 10_000
#: A body larger than this is not read up front, nor parsed, just to be logged.
READ_LIMIT = 1_000_000
#: Keys whose values are never stored, matched lowercased: these exactly, and
#: any key containing one of the parts.
SECRET_KEYS = {"access", "refresh", "otp", "pin", "authorization", "api_key", "apikey"}
SECRET_KEY_PARTS = ("password", "secret", "token")
HIDDEN = "[hidden]"


class ApiCallLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _is_recorded(request):
            return self.get_response(request)

        started_at = timezone.now()
        started = time.monotonic()
        sent = _read_body(request)
        response = self.get_response(request)
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            with transaction.atomic():
                ApiCall.objects.create(**_describe(request, response, started_at, duration_ms, sent))
        except Exception:
            logger.exception("api_log: could not record %s %s", request.method, request.path)
        return response


def _is_recorded(request):
    return (
        getattr(settings, "API_LOG_ENABLED", True)
        and request.method != "OPTIONS"
        and request.path.startswith(RECORDED_PREFIX)
        and not request.path.startswith(SKIPPED_PREFIXES)
    )


def _read_body(request):
    """A write's JSON or form body, read before the view consumes the stream."""
    if request.method not in WRITE_METHODS:
        return None
    if request.content_type not in ("application/json", "application/x-www-form-urlencoded"):
        return None
    length = _content_length(request)
    if not length or length > READ_LIMIT:
        return None
    try:
        return request.body
    except Exception:
        # The view will meet the same trouble reading it, and answer for it.
        return None


def _describe(request, response, started_at, duration_ms, sent):
    resolved = request.resolver_match  # None when no URL matched
    view = _view_path(resolved.func) if resolved else ""
    user = getattr(request, "user", None)
    is_write = request.method in WRITE_METHODS
    return dict(
        started_at=started_at,
        user=user if user is not None and user.is_authenticated else None,
        company_code=request.headers.get("Company-Code", "")[:32],
        method=request.method[:10],
        path=request.path[:500],
        query_string=_query_string(request),
        route=(resolved.route if resolved else "")[:300],
        view=view[:255],
        app_label=view.split(".", 1)[0][:60],
        status_code=response.status_code,
        duration_ms=duration_ms,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:300],
        request_bytes=_content_length(request),
        response_bytes=None if response.streaming else len(response.content),
        request_body=_request_body(request, sent) if is_write else "",
        response_body=_response_body(response) if is_write or response.status_code >= 400 else "",
    )


def _view_path(func):
    """``grpo.views.GRPOPostView`` for a class-based view or a viewset."""
    func = getattr(func, "view_class", None) or getattr(func, "cls", None) or func
    return f"{func.__module__}.{getattr(func, '__name__', type(func).__name__)}"


def _query_string(request):
    if not any(_is_secret(key) for key in request.GET):
        return request.META.get("QUERY_STRING", "")[:BODY_LIMIT]
    params = request.GET.copy()
    for key in params:
        if _is_secret(key):
            params.setlist(key, [HIDDEN])
    return params.urlencode()[:BODY_LIMIT]


def _request_body(request, sent):
    content_type = request.content_type or ""
    if sent is not None:
        text = sent.decode("utf-8", "replace")
        if content_type == "application/json":
            return _json_text(text)
        return _dump(_redact(_form_fields(QueryDict(text))))
    if content_type == "multipart/form-data":
        # Whatever DRF parsed it into; never parsed again here.
        fields = request.__dict__.get("_post")
        files = request.__dict__.get("_files")
        if fields is not None or files is not None:
            sent_files = {
                key: [f"{upload.name} ({upload.size} bytes)" for upload in files.getlist(key)]
                for key in (files or {})
            }
            form = _form_fields(fields) if fields is not None else {}
            return _dump({**_redact(form), "files": sent_files})
    length = _content_length(request)
    return f"[{content_type or 'body'}, {length} bytes, not kept]" if length else ""


def _response_body(response):
    if response.streaming:
        return "[streamed]"
    content_type = response.get("Content-Type", "")
    if not content_type.startswith(("application/json", "text/")):
        return f"[{content_type or 'body'}, {len(response.content)} bytes, not kept]"
    text = response.content.decode(response.charset or "utf-8", "replace")
    if "json" in content_type and len(response.content) <= READ_LIMIT:
        return _json_text(text)
    return _cut(text)


def _json_text(text):
    try:
        return _dump(_redact(json.loads(text)))
    except ValueError:
        return _cut(text)


def _form_fields(form):
    """A form's fields as a dict: one value as itself, several as a list."""
    return {key: values[0] if len(values) == 1 else values for key, values in form.lists()}


def _redact(value):
    if isinstance(value, dict):
        return {key: HIDDEN if _is_secret(key) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_secret(key):
    key = str(key).lower()
    return key in SECRET_KEYS or any(part in key for part in SECRET_KEY_PARTS)


def _dump(value):
    return _cut(json.dumps(value, ensure_ascii=False, default=str))


def _cut(text):
    if len(text) <= BODY_LIMIT:
        return text
    return f"{text[:BODY_LIMIT]}... [{len(text)} characters, cut]"


def _content_length(request):
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0) or None
    except ValueError:
        return None


def _client_ip(request):
    """The address nginx saw: the last X-Forwarded-For hop (as the partner
    onboarding throttle reads it), else the socket's."""
    hops = [hop.strip() for hop in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if hop.strip()]
    address = hops[-1] if hops else request.META.get("REMOTE_ADDR", "")
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return None
