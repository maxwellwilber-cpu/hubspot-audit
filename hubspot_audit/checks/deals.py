"""Deal pipeline hygiene.

Open, closed and won are resolved through hubspot_audit.dealstage rather than
by reading stage names, because a portal is free to rename and reorder every
stage and because the pipeline metadata flag this used to trust turns out not
to be part of HubSpot's published contract. See that module for the detail.
"""

from datetime import datetime, timedelta, timezone

from ..dealstage import OPTIONAL_PROPERTIES, StageResolver
from ..models import Finding, Severity
from ..normalize import clean
from ..phrasing import have, is_are, n_of
from .base import Check

#: An open deal untouched for this long is not a live deal, it is a ghost.
#: Deliberately generous. Long enterprise cycles exist, and a false "your
#: pipeline is fake" claim is the fastest way to lose a prospect's trust.
STALE_ACTIVITY_DAYS = 90

#: Above this, an all-digit timestamp must be milliseconds. 10^11 milliseconds
#: is March 1973; 10^11 seconds is the year 5138. Nothing real sits near the
#: boundary, so the two units can be told apart by magnitude.
_MILLIS_THRESHOLD = 10 ** 11

#: Below this, an all-digit value is implausible as an epoch in either unit --
#: 10^8 seconds is March 1973 and 10^8 milliseconds is two days after the epoch.
#: Returning None beats inventing a date in 1970 that the staleness check then
#: reports as a deal untouched for half a century.
_EPOCH_FLOOR = 10 ** 8


def _parse_ts(value):
    """HubSpot timestamps: ISO 8601 with a Z suffix, or epoch milliseconds.

    The epoch branch checks magnitude rather than assuming milliseconds. Legacy
    v1 exports and several sync integrations write epoch SECONDS into these
    fields, and dividing those by 1000 puts a live 2024 deal in January 1970 --
    which the staleness check would then report as untouched for fifty-five
    years, in a report whose whole promise is that the numbers are defensible.
    """
    text = clean(value)
    if not text:
        return None

    # str.isdigit() is True for non-ASCII digit characters, which int() then
    # parses into a number nobody wrote. A leading minus is a pre-1970 epoch.
    body = text[1:] if text.startswith("-") else text
    if body.isascii() and body.isdigit():
        # A bare YYYYMMDD is a date some integrations write, and as an epoch it
        # lands in 1970. Try it as a date before treating digits as an epoch.
        if len(body) == 8 and not text.startswith("-"):
            try:
                return datetime.strptime(body, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        try:
            number = int(text)
        except ValueError:
            return None
        if abs(number) < _EPOCH_FLOOR:
            return None
        seconds = number / 1000.0 if abs(number) >= _MILLIS_THRESHOLD else float(number)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp compared against an aware one raises. Assume UTC, which
    # is what HubSpot serves.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class _OpenDealCheck(Check):
    """Shared plumbing for the checks that only apply to open deals."""

    object_type = "deals"
    optional_properties = OPTIONAL_PROPERTIES

    def __init__(self):
        self.ids = []
        self._resolver = None

    def requires(self, profile):
        base = super().requires(profile)
        if base:
            return base
        self._resolver = StageResolver(profile)
        return self._resolver.open_reason()

    def observe(self, record):
        if not self._resolver.is_open(record):
            return
        self.inspect(record)

    def inspect(self, record):  # pragma: no cover - overridden by every subclass
        raise NotImplementedError

    def stage_rule(self, tail):
        return "deal is open according to the portal's %s, and %s" % (
            self._resolver.source, tail)


class OpenDealPastCloseDate(_OpenDealCheck):
    check_id = "deals.past_close_date"
    title = "Open deals whose close date has passed"
    severity = Severity.HIGH
    needs_properties = ("dealstage", "closedate")

    def __init__(self, now=None):
        super().__init__()
        self._now = now or datetime.now(timezone.utc)

    def inspect(self, record):
        close_date = _parse_ts(self.prop(record, "closedate"))
        if close_date and close_date < self._now:
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s past %s close date"
            % (n_of(len(self.ids), "open deal"), is_are(len(self.ids)),
               "its" if len(self.ids) == 1 else "their"),
            "Still sitting in an open stage with a close date in the past. "
            "Every forecast built on this pipeline is counting revenue from "
            "deals that already did or did not happen.",
            self.object_type, self.ids,
            rule=self.stage_rule("closedate is earlier than now"))]


class DealNoOwner(Check):
    check_id = "deals.no_owner"
    title = "Deals with no owner"
    object_type = "deals"
    severity = Severity.HIGH
    needs_properties = ("hubspot_owner_id",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        if not clean(self.prop(record, "hubspot_owner_id")):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s no owner" % (n_of(len(self.ids), "deal"), have(len(self.ids))),
            "Nobody is working these and they appear in no rep's pipeline. "
            "They also silently distort any quota or territory report.",
            self.object_type, self.ids,
            rule="hubspot_owner_id is empty")]


class DealNoAmount(_OpenDealCheck):
    check_id = "deals.no_amount"
    title = "Open deals with no value"
    severity = Severity.MEDIUM
    needs_properties = ("amount", "dealstage")

    def inspect(self, record):
        raw = clean(self.prop(record, "amount"))
        if not raw:
            self.ids.append(record["id"])
            return
        try:
            if float(raw) <= 0:
                self.ids.append(record["id"])
        except ValueError:
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s no value on %s"
            % (n_of(len(self.ids), "open deal"), have(len(self.ids)),
               "it" if len(self.ids) == 1 else "them"),
            "Blank, zero or unparseable amount. These contribute nothing to "
            "the pipeline total, so the forecast understates by however much "
            "they are actually worth.",
            self.object_type, self.ids,
            rule=self.stage_rule("amount is empty, zero, negative, or not a number"))]


class DealNoCloseDate(_OpenDealCheck):
    check_id = "deals.no_close_date"
    title = "Open deals with no close date"
    severity = Severity.MEDIUM
    needs_properties = ("closedate", "dealstage")

    def inspect(self, record):
        if not _parse_ts(self.prop(record, "closedate")):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s no close date"
            % (n_of(len(self.ids), "open deal"), have(len(self.ids))),
            "A deal with no close date lands in no period, so it is missing "
            "from every monthly and quarterly forecast while still looking "
            "like active pipeline on the board.",
            self.object_type, self.ids,
            rule=self.stage_rule("closedate is empty or unparseable"))]


class StaleOpenDeal(_OpenDealCheck):
    check_id = "deals.stale"
    title = "Open deals with no recent activity"
    severity = Severity.MEDIUM
    needs_properties = ("dealstage", "hs_lastmodifieddate")

    def __init__(self, now=None, days=STALE_ACTIVITY_DAYS):
        super().__init__()
        self.days = days
        self._now = now or datetime.now(timezone.utc)

    def inspect(self, record):
        # Prefer the record envelope's updatedAt; fall back to the property.
        touched = _parse_ts(record.get("updatedAt")) or _parse_ts(
            self.prop(record, "hs_lastmodifieddate"))
        if touched is None:
            return  # unknown is not the same as stale
        if touched < self._now - timedelta(days=self.days):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s not been touched in %d days"
            % (n_of(len(self.ids), "open deal"), have(len(self.ids)), self.days),
            "Nothing has changed on these records in over a quarter while they "
            "sit in an open stage. They are inflating the pipeline number "
            "without being worked.",
            self.object_type, self.ids,
            rule=self.stage_rule("it was last modified more than %d days ago" % self.days),
            evidence={"threshold_days": self.days})]


def build(profile, now=None):
    return [
        OpenDealPastCloseDate(now=now),
        DealNoOwner(),
        DealNoAmount(),
        DealNoCloseDate(),
        StaleOpenDeal(now=now),
    ]
