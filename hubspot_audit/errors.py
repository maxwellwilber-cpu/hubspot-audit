"""Typed errors for everything the HubSpot API can do to you.

Every one of these carries the HTTP status and the parsed error body, because
when a client runs this against their own portal the useful thing is not a
stack trace, it is a sentence telling them which scope they forgot to tick.
"""


class HubSpotError(Exception):
    """Base for every error raised by the client."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body or {}

    @property
    def correlation_id(self):
        """HubSpot stamps a correlationId on errors. Support asks for it."""
        return self.body.get("correlationId")


class AuthError(HubSpotError):
    """401. The token is missing, malformed, revoked, or from another portal."""


class ScopeError(HubSpotError):
    """403. The token is valid but the private app lacks a required scope.

    This is the single most common setup mistake, so the message names the
    scope the caller was trying to use rather than echoing HubSpot's generic
    'this app hasn't been granted all required scopes'.
    """

    def __init__(self, message, status=None, body=None, required_scope=None):
        super().__init__(message, status, body)
        self.required_scope = required_scope


class RateLimitError(HubSpotError):
    """429. Raised only after the client has exhausted its retries.

    policy is 'SECONDLY' or 'DAILY' from HubSpot's policyName field. They mean
    very different things: secondly is a slow-down, daily is a stop-for-today.
    """

    def __init__(self, message, status=None, body=None, policy=None):
        super().__init__(message, status, body)
        self.policy = policy or body.get("policyName") if body else policy

    @property
    def is_daily(self):
        return (self.policy or "").upper() == "DAILY"


class NotFoundError(HubSpotError):
    """404. Usually an object type this portal does not have."""


class ServerError(HubSpotError):
    """5xx after retries. HubSpot's problem, not the caller's."""


class TransportError(HubSpotError):
    """DNS failure, TLS failure, connection reset, timeout."""


class NoAuditableObjects(HubSpotError):
    """Nothing could be read, so there is nothing to audit.

    Raised instead of returning a report in which every check was skipped. A
    reader who sees no findings concludes there are none, so an audit that
    examined zero records has to fail rather than render.
    """
