"""The report must reconcile exactly to the findings behind it."""

import csv
import io
import json

from hubspot_audit.audit import run_audit
from hubspot_audit.client import HubSpotClient
from hubspot_audit.models import Status
from hubspot_audit.report import (
    render_csv, render_json, render_markdown, render_terminal,
)
from fake_portal import FakePortal
from planted import clean_portal, contacts_only_portal, dirty_portal


def audit(state):
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        return run_audit(client)


def test_csv_has_exactly_one_row_per_affected_record_per_finding():
    report = audit(dirty_portal())
    rows = list(csv.DictReader(io.StringIO(render_csv(report))))
    # Findings whose ids are property names are not part of a record work
    # order, so they are excluded from the CSV by design.
    expected = sum(f.count for f in report.findings if f.counts_records)
    assert len(rows) == expected
    assert expected > 0


def test_every_csv_row_carries_the_rule_that_produced_it():
    report = audit(dirty_portal())
    rows = list(csv.DictReader(io.StringIO(render_csv(report))))
    assert rows
    for row in rows:
        assert row["rule"].strip(), row
        assert row["check_id"].strip()
        assert row["severity"] in {"HIGH", "MEDIUM", "LOW"}


def test_csv_record_ids_match_the_findings_exactly():
    report = audit(dirty_portal())
    rows = list(csv.DictReader(io.StringIO(render_csv(report))))
    from_csv = {(r["check_id"], r["record_id"]) for r in rows}
    from_findings = {(f.check_id, rid) for f in report.findings
                     if f.counts_records for rid in f.record_ids}
    assert from_csv == from_findings


def test_headline_counts_records_not_findings():
    """A contact with two problems is one dirty record, not two.

    The headline number is what a client repeats back, so inflating it by
    double counting would be the single most damaging thing this tool could do
    to its own credibility.
    """
    report = audit(dirty_portal())
    affected = report.total_affected_records
    total_finding_hits = sum(f.count for f in report.findings
                             if f.object_type == "contacts")
    assert affected["contacts"] < total_finding_hits
    distinct = set()
    for finding in report.findings:
        if finding.object_type == "contacts" and finding.counts_records:
            distinct.update(finding.record_ids)
    assert affected["contacts"] == len(distinct)


def test_the_headline_can_never_exceed_the_number_of_records_in_the_portal():
    """A dead custom property is not a dirty contact.

    Those findings carry property NAMES in record_ids. Counting them as
    records once produced '15 contacts have at least one problem' on a portal
    holding 14 contacts, which is the kind of arithmetic that ends a sales
    conversation.
    """
    state = dirty_portal()
    report = audit(state)
    for object_type, count in report.total_affected_records.items():
        assert count <= len(state.objects[object_type]), object_type
    assert "preferred_contact_method" not in str(report.total_affected_records)


def test_a_sampled_run_says_so_in_every_output():
    """A partial audit that renders identically to a complete one is the worst
    output this tool could produce."""
    with FakePortal(dirty_portal()) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        report = run_audit(client, max_records=3)
    assert report.sampled == 3
    assert "PARTIAL SCAN" in render_terminal(report)
    assert "partial scan" in render_markdown(report).lower()
    assert json.loads(render_json(report))["sampled"] == 3


def test_a_complete_run_carries_no_sampling_banner():
    report = audit(dirty_portal())
    assert report.sampled is None
    assert "PARTIAL SCAN" not in render_terminal(report)
    assert "partial scan" not in render_markdown(report).lower()


def test_joining_checks_are_skipped_when_sampling_rather_than_inventing_findings():
    """Truncating contacts while reading every company would report companies
    as orphaned when their contacts were simply never read.

    Only checks that actually JOIN two streams are affected. A check reading one
    stream's own embedded associations is no more truncated than any
    single-object check, so it keeps running.
    """
    with FakePortal(dirty_portal()) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        report = run_audit(client, max_records=3)
    by_id = {r.check_id: r for r in report.results}

    for check_id in ("crossobject.lifecycle_contradicts_deal",
                     "crossobject.company_without_contacts"):
        assert by_id[check_id].status == Status.NOT_RUN, check_id
        assert "max-records" in by_id[check_id].reason

    # These read only the deals stream, so sampling does not distort them and
    # skipping would throw away a HIGH-severity result for nothing.
    for check_id in ("crossobject.deal_without_contact",
                     "crossobject.deal_without_company"):
        assert by_id[check_id].status != Status.NOT_RUN, check_id


def test_a_multi_stream_check_reports_not_run_when_any_of_its_streams_is_empty():
    """A portal with contacts and no deal records.

    Checking only the stream a check reports against left the other half
    invisible: this rendered as "Won deals whose contacts are not marked
    customers (18 contacts examined)" under "Checks that came back clean",
    having examined no deals at all.
    """
    state = dirty_portal()
    state.objects["deals"] = []
    report = audit(state)
    result = [r for r in report.results
              if r.check_id == "crossobject.lifecycle_contradicts_deal"][0]
    assert result.status == Status.NOT_RUN
    assert "deals" in result.reason


def test_markdown_lists_every_finding_title():
    report = audit(dirty_portal())
    markdown = render_markdown(report)
    assert report.findings
    for finding in report.findings:
        assert finding.title in markdown


def test_markdown_always_discloses_checks_that_could_not_run():
    report = audit(contacts_only_portal())
    markdown = render_markdown(report)
    assert "could not run" in markdown
    assert report.not_run
    for result in report.not_run:
        assert result.reason in markdown


def test_terminal_output_discloses_not_run_checks_too():
    report = audit(contacts_only_portal())
    text = render_terminal(report)
    assert "NOT RUN" in text
    for result in report.not_run:
        assert result.reason in text


def test_unused_properties_are_labelled_as_properties_not_record_ids():
    report = audit(dirty_portal())
    markdown = render_markdown(report)
    assert "Properties: `preferred_contact_method`" in markdown


def test_a_clean_portal_says_so_plainly():
    report = audit(clean_portal())
    text = render_terminal(report)
    assert "Nothing to report" in text
    assert not report.failures


def test_json_round_trips_and_carries_the_rules():
    report = audit(dirty_portal())
    payload = json.loads(render_json(report))
    assert payload["summary"]["checks_run"] == len(report.results)
    # Every rendered rule must match the rule on the finding it came from,
    # not merely be non-empty.
    rendered = {(f["check_id"], f["rule"])
                for r in payload["results"] for f in r["findings"]}
    actual = {(f.check_id, f.rule) for f in report.findings}
    assert rendered == actual
    assert actual


def test_json_summary_counts_match_the_result_statuses():
    report = audit(dirty_portal())
    payload = json.loads(render_json(report))
    statuses = [r["status"] for r in payload["results"]]
    assert payload["summary"]["passed"] == statuses.count(Status.PASS)
    assert payload["summary"]["failed"] == statuses.count(Status.FAIL)
    assert payload["summary"]["not_run"] == statuses.count(Status.NOT_RUN)
    assert payload["summary"]["errored"] == statuses.count(Status.ERROR)


def test_findings_are_ordered_worst_first():
    report = audit(dirty_portal())
    severities = [f.severity for f in report.findings]
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    assert severities == sorted(severities, key=lambda s: order[s])
