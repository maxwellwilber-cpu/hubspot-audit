"""The runner: discover the portal, page each object type once, feed every check.

One pass per object type. Every check that asked for that stream sees each
record as it goes past, then all of them are finished together. The alternative
-- a scan per check -- would multiply API calls by roughly twenty and make a
large portal impossible to audit inside the rate limit.
"""

from datetime import datetime, timezone

from .checks import companies as company_checks
from .checks import contacts as contact_checks
from .checks import crossobject as cross_checks
from .checks import deals as deal_checks
from .checks import properties as property_checks
from .checks.base import run_check
from .checks.crossobject import CrossCheck
from .errors import HubSpotError
from .models import AuditReport, errored, not_run
from .schema import OBJECT_SCOPES, discover

OBJECT_ORDER = ("contacts", "companies", "deals")


def _streams_of(check):
    return tuple(getattr(check, "streams", ()) or (check.object_type,))


def _properties_for(check, stream):
    """Every property this check wants on this stream: required plus optional.

    Optional properties are requested but do not gate the check. They exist for
    the cases where a better data source may or may not be present in a given
    portal -- hs_is_closed on deals, for one.
    """
    if isinstance(check, CrossCheck):
        return (tuple(check.stream_properties.get(stream, ()))
                + tuple(check.stream_optional_properties.get(stream, ())))
    if stream != check.object_type:
        return ()
    return tuple(check.needs_properties) + tuple(check.optional_properties)


def _associations_for(check, stream):
    if isinstance(check, CrossCheck):
        return tuple(check.stream_associations.get(stream, ()))
    return tuple(check.needs_associations) if stream == check.object_type else ()


def build_checks(profile, now=None):
    """Every check this package knows about, wired for this portal."""
    return (contact_checks.build(profile)
            + company_checks.build(profile)
            + deal_checks.build(profile, now=now)
            + cross_checks.build(profile)
            + property_checks.build(profile))


def run_audit(client, profile=None, checks=None, now=None, max_records=None):
    """Run every check against the portal and return an AuditReport.

    max_records caps how many records are read per object type. That makes a
    large portal fast to sample, but it also truncates one stream while another
    is read in full, which would make cross-object checks invent findings: cap
    contacts at 500 and every company whose contacts fell past the cutoff looks
    like a company with no contacts. So cross-object checks are skipped, with a
    stated reason, whenever a cap is in force. The report also carries a
    sampled flag, because a partial audit that renders identically to a
    complete one is the worst output this tool could produce.
    """
    started = datetime.now(timezone.utc)
    profile = profile if profile is not None else discover(client)
    checks = checks if checks is not None else build_checks(profile, now=now)

    # requires() is called once here, before any paging, because a check that
    # cannot run should not cause its properties to be requested. Several
    # checks also resolve internal state (open stage ids, custom property
    # lists) inside requires(), so this has to happen first.
    skip_reasons = {}
    active = []
    for check in checks:
        try:
            reason = check.requires(profile)
        except Exception as exc:
            skip_reasons[check] = "requires() failed: %s: %s" % (type(exc).__name__, exc)
            continue
        if reason:
            skip_reasons[check] = reason
        elif max_records and isinstance(check, CrossCheck):
            skip_reasons[check] = (
                "skipped because --max-records truncates one object type and "
                "not another, which would make this check report records as "
                "unlinked when their counterparts were simply never read")
        else:
            active.append(check)

    # Work out, per object type, which properties and associations to request.
    wanted_properties = {obj: set() for obj in OBJECT_ORDER}
    wanted_associations = {obj: set() for obj in OBJECT_ORDER}
    subscribers = {obj: [] for obj in OBJECT_ORDER}

    for check in active:
        for stream in _streams_of(check):
            if stream not in subscribers:
                continue
            subscribers[stream].append(check)
            wanted_properties[stream].update(_properties_for(check, stream))
            wanted_associations[stream].update(_associations_for(check, stream))

    records_seen = {obj: 0 for obj in OBJECT_ORDER}
    stream_errors = {}

    for object_type in OBJECT_ORDER:
        if not subscribers[object_type]:
            continue
        if not profile.available(object_type):
            stream_errors[object_type] = profile.schema(object_type).reason
            continue

        params = {}
        if wanted_properties[object_type]:
            params["properties"] = ",".join(sorted(wanted_properties[object_type]))
        if wanted_associations[object_type]:
            params["associations"] = ",".join(sorted(wanted_associations[object_type]))

        try:
            stream = client.paginate(
                "/crm/v3/objects/%s" % object_type,
                params=params,
                required_scope=OBJECT_SCOPES.get(object_type))
            for record in stream:
                records_seen[object_type] += 1
                for check in subscribers[object_type]:
                    if isinstance(check, CrossCheck):
                        check.observe_stream(object_type, record)
                    else:
                        check.observe(record)
                if max_records and records_seen[object_type] >= max_records:
                    break
        except HubSpotError as exc:
            # One object type failing does not sink the audit. The checks that
            # depended on it report the failure instead of a false PASS.
            stream_errors[object_type] = "%s: %s" % (type(exc).__name__, exc)

    results = []
    for check in checks:
        if check in skip_reasons:
            results.append(not_run(check, skip_reasons[check]))
            continue

        streams = [s for s in _streams_of(check) if s in records_seen]
        broken = [s for s in streams if s in stream_errors]
        if broken:
            results.append(errored(
                check, "could not read %s (%s)"
                % (", ".join(broken), stream_errors[broken[0]])))
            continue

        # Strictly the stream this check reports against. Borrowing another
        # stream's count was how a cross-object check could report PASS on a
        # portal with zero contacts: "Won deals whose contacts are not marked
        # customers -- 200 contacts examined", on a portal with no contacts.
        seen = records_seen.get(check.object_type, 0)

        results.append(run_check(check, profile, seen, check.finish))

    return AuditReport(profile, results, started_at=started,
                       finished_at=datetime.now(timezone.utc),
                       requests_made=getattr(client, "request_count", 0),
                       sampled=max_records or None)
