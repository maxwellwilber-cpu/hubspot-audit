"""The shapes an audit produces.

The central design rule lives here: a check that could not run says so. It does
not pass. A green report that silently validated nothing is worse than a red
one, because the client acts on it.
"""

from datetime import datetime, timezone


class Severity:
    """Ranked by what it costs the business, not by how broken it looks.

    HIGH   costs money or breaks a process right now
    MEDIUM degrades reporting and decision-making
    LOW    hygiene, worth fixing during a cleanup but nothing is on fire
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

    @classmethod
    def rank(cls, severity):
        return cls.ORDER.get(severity, 99)


class Status:
    PASS = "PASS"        # ran, found nothing wrong
    FAIL = "FAIL"        # ran, found something
    NOT_RUN = "NOT_RUN"  # could not run, and says why
    ERROR = "ERROR"      # blew up; the audit continues, this check is honest about it


class Finding:
    """One problem, the rule that found it, and the records it affects."""

    def __init__(self, check_id, severity, title, detail, object_type,
                 record_ids=None, rule=None, evidence=None):
        self.check_id = check_id
        self.severity = severity
        self.title = title
        self.detail = detail
        self.object_type = object_type
        self.record_ids = list(record_ids or [])
        # The rule string is what makes a finding defensible. "37 duplicates"
        # invites an argument; "37 pairs matched on exact normalized email"
        # ends one.
        self.rule = rule or check_id
        self.evidence = evidence or {}

    @property
    def count(self):
        return len(self.record_ids)

    @property
    def counts_records(self):
        """False when record_ids holds something other than record ids."""
        return not str(self.evidence.get("unit", "")).startswith("property")

    def sample(self, n=10):
        return self.record_ids[:n]

    def to_dict(self):
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "object_type": self.object_type,
            "rule": self.rule,
            "count": self.count,
            "record_ids": self.record_ids,
            "evidence": self.evidence,
        }


class CheckResult:
    """What one check concluded, including the case where it concluded nothing."""

    def __init__(self, check_id, title, object_type, status, findings=None,
                 reason=None, records_examined=0, severity=Severity.MEDIUM):
        self.check_id = check_id
        self.title = title
        self.object_type = object_type
        self.status = status
        self.findings = list(findings or [])
        self.reason = reason
        self.records_examined = records_examined
        self.severity = severity

    @property
    def total_affected(self):
        return sum(f.count for f in self.findings)

    def to_dict(self):
        return {
            "check_id": self.check_id,
            "title": self.title,
            "object_type": self.object_type,
            "status": self.status,
            "reason": self.reason,
            "records_examined": self.records_examined,
            "severity": self.severity,
            "findings": [f.to_dict() for f in self.findings],
        }


def passed(check, records_examined):
    """A clean result. Refuses to exist if nothing was examined.

    This is the guard. Calling passed() with zero records examined is a
    programming error, because the honest answer in that case is NOT_RUN with
    a reason attached.
    """
    if records_examined <= 0:
        raise ValueError(
            "%s tried to report PASS after examining 0 records. A check that "
            "examined nothing must return not_run() with a reason." % check.check_id)
    return CheckResult(check.check_id, check.title, check.object_type,
                       Status.PASS, records_examined=records_examined,
                       severity=check.severity)


def failed(check, findings, records_examined):
    return CheckResult(check.check_id, check.title, check.object_type,
                       Status.FAIL, findings=findings,
                       records_examined=records_examined, severity=check.severity)


def not_run(check, reason):
    """Could not run. The reason is shown in the report, never swallowed."""
    return CheckResult(check.check_id, check.title, check.object_type,
                       Status.NOT_RUN, reason=reason, severity=check.severity)


def errored(check, reason):
    return CheckResult(check.check_id, check.title, check.object_type,
                       Status.ERROR, reason=reason, severity=check.severity)


class AuditReport:
    """Every check result plus enough context to reproduce the run."""

    def __init__(self, portal, results, started_at=None, finished_at=None,
                 requests_made=0, sampled=None):
        self.portal = portal
        self.results = list(results)
        #: None for a complete scan; otherwise the per-object-type cap that was
        #: applied. A sampled run must never render as a complete one.
        self.sampled = sampled
        self.started_at = started_at or datetime.now(timezone.utc)
        self.finished_at = finished_at or datetime.now(timezone.utc)
        self.requests_made = requests_made

    # -- rollups -----------------------------------------------------------

    def by_status(self, status):
        return [r for r in self.results if r.status == status]

    @property
    def failures(self):
        return sorted(self.by_status(Status.FAIL),
                      key=lambda r: (Severity.rank(r.severity), -r.total_affected))

    @property
    def not_run(self):
        return self.by_status(Status.NOT_RUN)

    @property
    def errors(self):
        return self.by_status(Status.ERROR)

    @property
    def findings(self):
        out = []
        for result in self.failures:
            out.extend(result.findings)
        return sorted(out, key=lambda f: (Severity.rank(f.severity), -f.count))

    @property
    def total_affected_records(self):
        """Distinct records with at least one problem, per object type.

        Deliberately not a sum of finding counts: one contact with a missing
        email AND no owner is one dirty record, not two. Reporting it as two
        would inflate the headline number, and the headline number is the one
        a client repeats back.
        """
        by_type = {}
        for finding in self.findings:
            # Some findings list property NAMES rather than record ids -- the
            # dead-custom-field check, for one. Counting those as records
            # inflates the headline above the number of records in the portal,
            # which is the fastest way to lose the trust this report exists to
            # build.
            if not finding.counts_records:
                continue
            by_type.setdefault(finding.object_type, set()).update(finding.record_ids)
        return {k: len(v) for k, v in sorted(by_type.items())}

    @property
    def duration_seconds(self):
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self):
        return {
            "portal": self.portal.to_dict() if self.portal else None,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "requests_made": self.requests_made,
            "sampled": self.sampled,
            "summary": {
                "checks_run": len(self.results),
                "passed": len(self.by_status(Status.PASS)),
                "failed": len(self.failures),
                "not_run": len(self.not_run),
                "errored": len(self.errors),
                "affected_records": self.total_affected_records,
            },
            "results": [r.to_dict() for r in self.results],
        }
