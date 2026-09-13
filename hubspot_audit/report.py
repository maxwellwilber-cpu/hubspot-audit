"""Turning an AuditReport into something a business owner will read.

Three outputs, for three different moments:
  terminal  -- what you look at while it runs
  markdown  -- what you send the client
  csv       -- the record ids, so somebody can actually fix them

Every number that appears in any of them traces back to a rule string and a
list of record ids. That is the whole point: "your CRM has 340 problems" is a
sales pitch, and "340 contacts share 118 email addresses, matched on exact
lowercased email, here are the ids" is a work order.
"""

import csv
import io
import json

from .models import Severity, Status

BAR = "=" * 74
RULE = "-" * 74


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


# -- terminal --------------------------------------------------------------

def render_terminal(report, color=False):
    lines = []
    portal = report.portal

    lines.append(BAR)
    lines.append("  HUBSPOT CRM AUDIT")
    lines.append(BAR)

    if portal:
        available = [name for name, schema in sorted(portal.schemas.items())
                     if schema.available]
        lines.append("  Objects audited:  %s" % (", ".join(available) or "none"))
        unavailable = [(name, schema.reason)
                       for name, schema in sorted(portal.schemas.items())
                       if not schema.available]
        for name, reason in unavailable:
            lines.append("  Skipped %-9s %s" % (name + ":", reason))
    lines.append("  API requests:     %d" % report.requests_made)
    if report.sampled:
        lines.append("  PARTIAL SCAN:     stopped at %d records per object type. "
                     "Counts below are not portal totals." % report.sampled)
    lines.append("  Duration:         %.1fs" % report.duration_seconds)
    lines.append("")

    summary = report.to_dict()["summary"]
    lines.append("  %d checks: %d passed, %d failed, %d not run, %d errored"
                 % (summary["checks_run"], summary["passed"], summary["failed"],
                    summary["not_run"], summary["errored"]))

    affected = report.total_affected_records
    if affected:
        parts = ["%d %s" % (count, object_type)
                 for object_type, count in affected.items()]
        lines.append("  Records with at least one problem: %s" % ", ".join(parts))
    lines.append("")

    if not report.failures:
        lines.append("  Nothing to report. Every check that ran came back clean.")
    for severity in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        findings = [f for f in report.findings if f.severity == severity]
        if not findings:
            continue
        lines.append(RULE)
        lines.append("  %s  (%d %s)" % (severity, len(findings),
                                        _plural(len(findings), "finding")))
        lines.append(RULE)
        for finding in findings:
            lines.append("  %s" % finding.title)
            lines.append("      rule: %s" % finding.rule)
            sample = finding.sample(5)
            if sample:
                more = finding.count - len(sample)
                suffix = " (+%d more)" % more if more > 0 else ""
                lines.append("      ids:  %s%s" % (", ".join(sample), suffix))
            lines.append("")

    skipped = report.not_run
    if skipped:
        lines.append(RULE)
        lines.append("  NOT RUN  (%d %s)" % (len(skipped), _plural(len(skipped), "check")))
        lines.append(RULE)
        # This section is not filler. A check that could not run has validated
        # nothing, and a reader who assumes silence means clean is being misled.
        for result in skipped:
            lines.append("  %s" % result.title)
            lines.append("      %s" % result.reason)
        lines.append("")

    errors = report.errors
    if errors:
        lines.append(RULE)
        lines.append("  ERRORED  (%d)" % len(errors))
        lines.append(RULE)
        for result in errors:
            lines.append("  %s: %s" % (result.title, result.reason))
        lines.append("")

    lines.append(BAR)
    return "\n".join(lines)


# -- markdown --------------------------------------------------------------

def render_markdown(report):
    out = []
    portal = report.portal
    summary = report.to_dict()["summary"]

    out.append("# HubSpot CRM audit")
    out.append("")
    out.append("Run %s. %d API requests, %.1f seconds."
               % (report.finished_at.strftime("%Y-%m-%d %H:%M UTC"),
                  report.requests_made, report.duration_seconds))
    out.append("")
    if report.sampled:
        out.append("> **This was a partial scan.** It stopped after %d records "
                   "per object type, so every count below is a sample and not a "
                   "portal total." % report.sampled)
        out.append("")

    affected = report.total_affected_records
    if affected:
        out.append("## What this found")
        out.append("")
        for object_type, count in affected.items():
            out.append("- **%d %s** have at least one problem" % (count, object_type))
        out.append("")
        out.append("A record with two problems is counted once here, so these "
                   "numbers are records to fix rather than issues to fix.")
        out.append("")

    out.append("%d checks ran: %d passed, %d failed, %d could not run, %d errored."
               % (summary["checks_run"], summary["passed"], summary["failed"],
                  summary["not_run"], summary["errored"]))
    out.append("")

    for severity in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        findings = [f for f in report.findings if f.severity == severity]
        if not findings:
            continue
        out.append("## %s" % {
            Severity.HIGH: "Costing money now",
            Severity.MEDIUM: "Degrading your reporting",
            Severity.LOW: "Worth cleaning up",
        }[severity])
        out.append("")
        for finding in findings:
            out.append("### %s" % finding.title)
            out.append("")
            out.append(finding.detail)
            out.append("")
            out.append("*How this was determined:* %s" % finding.rule)
            out.append("")
            sample = finding.sample(10)
            if sample and finding.evidence.get("unit", "").startswith("property"):
                out.append("Properties: `%s`" % "`, `".join(sample))
            elif sample:
                more = finding.count - len(sample)
                suffix = " and %d more" % more if more > 0 else ""
                out.append("Example record IDs: %s%s" % (", ".join(sample), suffix))
            out.append("")

    if report.not_run:
        out.append("## Checks that could not run")
        out.append("")
        out.append("These validated nothing. They are listed so that a clean "
                   "result above is not mistaken for coverage it does not have.")
        out.append("")
        for result in report.not_run:
            out.append("- **%s** — %s" % (result.title, result.reason))
        out.append("")

    if report.errors:
        out.append("## Checks that errored")
        out.append("")
        for result in report.errors:
            out.append("- **%s** — %s" % (result.title, result.reason))
        out.append("")

    passing = report.by_status(Status.PASS)
    if passing:
        out.append("## Checks that came back clean")
        out.append("")
        for result in sorted(passing, key=lambda r: r.check_id):
            out.append("- %s (%d %s examined)"
                       % (result.title, result.records_examined, result.object_type))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# -- csv -------------------------------------------------------------------

CSV_COLUMNS = ["object_type", "record_id", "check_id", "severity", "title", "rule"]


def render_csv(report):
    """One row per affected record per finding. This is the work order."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for finding in report.findings:
        # Findings whose ids are property names are not a record work order.
        if not finding.counts_records:
            continue
        for record_id in finding.record_ids:
            writer.writerow([finding.object_type, record_id, finding.check_id,
                             finding.severity, finding.title, finding.rule])
    return buffer.getvalue()


def render_json(report):
    return json.dumps(report.to_dict(), indent=2, sort_keys=False) + "\n"
