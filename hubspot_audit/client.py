"""Read-only HTTP client for the HubSpot CRM v3 API.

Standard library only. No requests, no hubspot-api-client, nothing to install.
That is deliberate: this tool asks people for a token to their CRM, and the
smallest possible dependency surface is part of what makes that a reasonable
thing to ask.

READ-ONLY BY CONSTRUCTION. This module can only issue GET requests. There is no
code path that sends POST, PATCH, PUT or DELETE, and tests/test_readonly.py
fails the build if one ever appears.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import (
    AuthError,
    HubSpotError,
    NotFoundError,
    RateLimitError,
    ScopeError,
    ServerError,
    TransportError,
)

DEFAULT_BASE_URL = "https://api.hubapi.com"

# HubSpot caps page size at 100 for the CRM object endpoints. Asking for more
# is not an error, it is silently clamped, which makes pagination bugs hard to
# see. So we clamp it ourselves and stay honest about the page size.
MAX_PAGE_SIZE = 100

# Burst limit is 100 requests / 10s on Free and Starter, 190 on Pro and
# Enterprise. We do not know the tier up front, so the default pacing targets
# the lowest tier and the client speeds up only if HubSpot's own headers say
# there is more room.
DEFAULT_REQUESTS_PER_INTERVAL = 100
DEFAULT_INTERVAL_MS = 10_000


class RateLimiter:
    """Paces requests against a rolling window, then backs off on 429 anyway.

    Two layers because neither alone is enough. Proactive pacing keeps a large
    portal scan from tripping the limit at all; reactive backoff handles the
    case where something else is also using the same private app's quota,
    which we cannot see from here.
    """

    def __init__(self, max_requests=DEFAULT_REQUESTS_PER_INTERVAL,
                 interval_ms=DEFAULT_INTERVAL_MS, sleep=time.sleep,
                 clock=time.monotonic):
        self.max_requests = max(1, int(max_requests))
        self.interval = max(0.001, interval_ms / 1000.0)
        self._sleep = sleep
        self._clock = clock
        self._times = []

    def observe_headers(self, headers):
        """Let HubSpot's own headers correct our guess about the tier."""
        try:
            limit = headers.get("X-HubSpot-RateLimit-Max")
            interval = headers.get("X-HubSpot-RateLimit-Interval-Milliseconds")
            if limit:
                self.max_requests = max(1, int(limit))
            if interval:
                self.interval = max(0.001, int(interval) / 1000.0)
        except (TypeError, ValueError):
            # Malformed headers are not worth failing a scan over.
            pass

    def acquire(self):
        now = self._clock()
        cutoff = now - self.interval
        self._times = [t for t in self._times if t > cutoff]
        if len(self._times) >= self.max_requests:
            # Sleep until the oldest request in the window ages out.
            wait = self._times[0] + self.interval - now
            if wait > 0:
                self._sleep(wait)
            now = self._clock()
            cutoff = now - self.interval
            self._times = [t for t in self._times if t > cutoff]
        self._times.append(now)


class HubSpotClient:
    """Issues GET requests against the CRM v3 API and pages through results."""

    def __init__(self, token, base_url=DEFAULT_BASE_URL, timeout=30,
                 max_retries=5, rate_limiter=None, sleep=time.sleep,
                 user_agent="hubspot-audit"):
        if not token or not str(token).strip():
            raise AuthError("No access token supplied. Set HUBSPOT_TOKEN or pass --token.")
        self._token = str(token).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._user_agent = user_agent
        self.limiter = rate_limiter if rate_limiter is not None else RateLimiter()
        self.request_count = 0

    # -- low level ---------------------------------------------------------

    def _build_url(self, path, params=None):
        url = self.base_url + path
        if params:
            # Drop None values so callers can pass optional params freely.
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        return url

    def _open(self, url):
        """One raw GET. Returns (status, headers, parsed_body)."""
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", "Bearer " + self._token)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", self._user_agent)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, dict(response.headers), _parse(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, dict(exc.headers or {}), _parse(raw)
        except urllib.error.URLError as exc:
            raise TransportError("Could not reach HubSpot: %s" % exc.reason) from exc
        except TimeoutError as exc:
            raise TransportError("Request to HubSpot timed out after %ss" % self.timeout) from exc

    def get(self, path, params=None, required_scope=None):
        """GET with pacing, retries and a typed error for every failure mode."""
        url = self._build_url(path, params)
        attempt = 0
        while True:
            self.limiter.acquire()
            status, headers, body = self._open(url)
            self.request_count += 1
            self.limiter.observe_headers(headers)

            if 200 <= status < 300:
                return body

            if status == 429:
                policy = (body.get("policyName") or "").upper()
                # A daily limit will not clear by waiting a few seconds. Fail
                # immediately rather than sleeping through the retry budget.
                if policy == "DAILY" or attempt >= self.max_retries:
                    raise RateLimitError(
                        body.get("message") or "HubSpot rate limit reached.",
                        status, body, policy=policy)
                self._sleep(_retry_delay(attempt, headers))
                attempt += 1
                continue

            if status >= 500:
                if attempt >= self.max_retries:
                    raise ServerError(
                        body.get("message") or "HubSpot returned %s." % status, status, body)
                self._sleep(_retry_delay(attempt, headers))
                attempt += 1
                continue

            if status == 401:
                raise AuthError(
                    "HubSpot rejected the token (401). Check that the private app token is "
                    "current and belongs to this portal.", status, body)

            if status == 403:
                hint = " Missing scope: %s." % required_scope if required_scope else ""
                raise ScopeError(
                    "HubSpot refused the request (403). The private app is missing a "
                    "required read scope.%s" % hint, status, body,
                    required_scope=required_scope)

            if status == 404:
                raise NotFoundError(
                    body.get("message") or "Not found: %s" % path, status, body)

            raise HubSpotError(
                body.get("message") or "Unexpected HubSpot response %s." % status, status, body)

    # -- paging ------------------------------------------------------------

    def paginate(self, path, params=None, page_size=MAX_PAGE_SIZE,
                 required_scope=None, max_pages=None):
        """Yield records one at a time, following paging.next.after.

        Yields records rather than returning a list so a 200,000-contact portal
        does not have to fit in memory. Callers that genuinely need everything
        can still list() it, but no check in this package does.
        """
        params = dict(params or {})
        params["limit"] = min(int(page_size), MAX_PAGE_SIZE)
        after = None
        pages = 0
        seen_cursors = set()

        while True:
            if after is not None:
                params["after"] = after
            body = self.get(path, params, required_scope=required_scope)
            results = body.get("results") or []
            for record in results:
                yield record

            pages += 1
            if max_pages is not None and pages >= max_pages:
                return

            after = (body.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return

            # A portal that returns the same cursor twice would spin forever.
            # Seen in the wild when a sync job is mutating records mid-scan.
            if after in seen_cursors:
                return
            seen_cursors.add(after)

            # An empty page with a cursor is legal but if it repeats we would
            # also spin. The cursor check above covers it; this is belt and
            # braces for a page that returns nothing and a fresh cursor.
            if not results and pages > 1000:
                return


def _parse(raw):
    """HubSpot is JSON everywhere, but error pages from proxies are not."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"message": raw[:400].decode("utf-8", "replace")}
    # A JSON array or scalar body is not something we can .get() on.
    return parsed if isinstance(parsed, dict) else {"results": parsed}


def _retry_delay(attempt, headers):
    """Exponential backoff, but prefer the interval HubSpot tells us about."""
    base = 1.0
    try:
        interval_ms = headers.get("X-HubSpot-RateLimit-Interval-Milliseconds")
        if interval_ms:
            base = max(0.05, int(interval_ms) / 1000.0)
    except (TypeError, ValueError):
        pass
    # 1x, 2x, 4x, 8x... capped so a scan cannot hang for minutes.
    return min(base * (2 ** attempt), 60.0)
