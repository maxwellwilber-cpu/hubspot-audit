"""The check contract.

Checks are streaming collectors, not functions over a list. Each object type is
paged through the API exactly once and every check for that type sees each
record as it goes by. The alternative -- one scan per check -- would multiply
API calls by the number of checks and make a large portal unauditable inside
the rate limit.

The contract, in order:
    requires(profile) -> reason string, or None to proceed
    observe(record)   -> called once per record, returns nothing
    finish()          -> list of Finding

A check must not report PASS after seeing zero records. models.passed()
enforces that, and the runner turns an empty scan into NOT_RUN with a reason.
"""

from ..models import Severity, errored, failed, not_run, passed


class CheckNotApplicable(Exception):
    """Raised from finish() when a check ran but could not classify anything.

    Reaching the end of a scan having been unable to make the judgement the
    check exists to make is not a clean result, and returning no findings would
    render as one. The message becomes the NOT_RUN reason.
    """


class Check:
    check_id = "unset"
    title = "unset"
    object_type = "contacts"
    severity = Severity.MEDIUM

    #: Properties this check reads. The runner unions these across all checks
    #: for the object type and asks HubSpot for exactly that set, because the
    #: v3 list endpoints return only a small default selection otherwise --
    #: which would leave every check silently looking at absent fields.
    needs_properties = ()

    #: Properties requested from the API when the portal has them, but which do
    #: not gate the check. Used where a better data source may or may not exist.
    optional_properties = ()

    #: Associated object types this check needs returned with each record.
    needs_associations = ()

    def requires(self, profile):
        """Return a reason string if this check cannot run against this portal.

        Default: the object type must exist and every needed property must be
        defined in this portal's schema.
        """
        schema = profile.schema(self.object_type)
        if not schema.available:
            return schema.reason or "this portal has no %s object" % self.object_type
        missing = schema.missing(*self.needs_properties)
        if missing:
            return "this portal has no %s property on %s" % (
                ", ".join(missing), self.object_type)
        return None

    def observe(self, record):  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    def finish(self):  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    # -- helpers for subclasses -------------------------------------------

    @staticmethod
    def prop(record, name, default=""):
        value = (record.get("properties") or {}).get(name)
        return default if value is None else value

    @staticmethod
    def associated_ids(record, object_type):
        block = (record.get("associations") or {}).get(object_type) or {}
        return [str(r.get("id")) for r in block.get("results") or [] if r.get("id")]


def run_check(check, profile, records_seen, build_findings):
    """Shared result-building so every check handles the edge cases the same.

    records_seen is the count the runner observed for this object type.
    build_findings is a zero-argument callable returning a list of Finding.

    requires() is NOT called here. The runner calls it exactly once, before any
    records are observed, and skips the check if it returns a reason. Calling
    it a second time after observation would be wrong, because several checks
    initialise counters inside requires() -- a second call silently resets
    everything they just counted, and the check then reports a clean portal as
    entirely empty. tests/test_checks.py pins the call count at one.
    """
    if records_seen == 0:
        return not_run(check, "no %s records exist in this portal" % check.object_type)
    try:
        findings = [f for f in build_findings() if f.count]
    except CheckNotApplicable as exc:
        return not_run(check, str(exc))
    except Exception as exc:  # a broken check must not kill the audit
        return errored(check, "%s: %s" % (type(exc).__name__, exc))
    if findings:
        return failed(check, findings, records_seen)
    return passed(check, records_seen)
