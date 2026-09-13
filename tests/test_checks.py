"""The checks themselves.

Three things have to hold, and the last one is the important one:
  1. every planted defect is caught by the check that claims to catch it
  2. every check can return PASS, proved against a portal with no defects
  3. no check can pass vacuously -- a check that examined nothing says NOT_RUN
"""

import pytest

from hubspot_audit.audit import build_checks, run_audit
from hubspot_audit.client import HubSpotClient
from hubspot_audit.models import Status
from hubspot_audit.schema import discover
from fake_portal import FakePortal
from planted import (
    PLANTED, clean_portal, contacts_only_portal, custom_lifecycle_portal,
    dirty_portal, no_pipeline_portal,
)


def audit(state, **kwargs):
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        return run_audit(client, **kwargs)


@pytest.fixture(scope="module")
def dirty_report():
    return audit(dirty_portal())


@pytest.fixture(scope="module")
def clean_report():
    return audit(clean_portal())


def result_for(report, check_id):
    for result in report.results:
        if result.check_id == check_id:
            return result
    raise AssertionError("no check produced results for %r. Present: %s"
                         % (check_id, sorted(r.check_id for r in report.results)))


def ids_for(report, check_id):
    found = set()
    for finding in result_for(report, check_id).findings:
        found.update(finding.record_ids)
    return found


# -- 1. every planted defect is caught -------------------------------------

@pytest.mark.parametrize("check_id,expected", sorted(PLANTED.items()))
def test_planted_defect_is_caught_by_the_right_check(dirty_report, check_id, expected):
    result = result_for(dirty_report, check_id)
    assert result.status == Status.FAIL, (
        "%s did not fail on the dirty portal (status %s, reason %s)"
        % (check_id, result.status, result.reason))
    assert ids_for(dirty_report, check_id) == set(expected)


def test_the_dirty_portal_trips_exactly_the_checks_we_planted_for(dirty_report):
    """Nothing fails that we did not plant. Catches over-eager rules.

    No exemption list. Every check that fails here has an entry in PLANTED with
    the exact ids it should report, so a rule that widens its net fails this.
    """
    failing = {r.check_id for r in dirty_report.failures}
    assert failing == set(PLANTED)


def test_phone_format_inconsistency_names_the_minority_formats(dirty_report):
    result = result_for(dirty_report, "contacts.phone_format_inconsistency")
    assert result.status == Status.FAIL
    evidence = result.findings[0].evidence
    assert evidence["dominant"] == "parenthesized"
    assert evidence["formats"]["dashed"] == 2


def test_unused_custom_property_is_reported_by_name_not_by_record_id(dirty_report):
    result = result_for(dirty_report, "schema.unused_custom_properties.contacts")
    assert result.status == Status.FAIL
    finding = result.findings[0]
    # legacy_import_batch is populated on every contact and must not appear.
    assert finding.record_ids == ["preferred_contact_method"]
    assert finding.evidence["unit"] == "property names, not record ids"
    assert not finding.counts_records


def test_duplicate_email_is_matched_case_and_whitespace_insensitively(dirty_report):
    # Contact 2's address is " DUPE@acme.test " with different case and padding.
    found = ids_for(dirty_report, "contacts.duplicate_email")
    assert {"1", "2"} <= found
    groups = dirty_report.results and result_for(
        dirty_report, "contacts.duplicate_email").findings[0].evidence
    assert groups["duplicate_groups"] == 3


def test_a_pair_already_reported_as_an_email_duplicate_is_not_reported_twice(dirty_report):
    """Contacts 1 and 2 share an email, a surname AND a phone number.

    They form a name+phone group, so the exclusion branch is actually reached
    rather than being trivially satisfied. They must appear under the email
    rule only.
    """
    assert {"1", "2"} <= ids_for(dirty_report, "contacts.duplicate_email")
    assert not ({"1", "2"} & ids_for(dirty_report, "contacts.duplicate_name_phone"))


def test_a_pair_spanning_two_email_groups_is_still_reported_as_a_duplicate(dirty_report):
    """The false negative that a flat "already seen" id set produces.

    Contacts 15 and 16 are one person under two addresses. Each of those
    addresses is separately duplicated by another contact, so all four ids sit
    in SOME email collision -- but 15 and 16 were never reported as a pair. An
    exclusion keyed on id membership drops them and hides a real duplicate; one
    keyed on group identity keeps them.
    """
    assert {"15", "16"} <= ids_for(dirty_report, "contacts.duplicate_name_phone")


def test_lifecycle_contradiction_uses_the_portals_own_won_stage(dirty_report):
    result = result_for(dirty_report, "crossobject.lifecycle_contradicts_deal")
    assert result.findings[0].record_ids == ["14"]


def test_closed_deals_are_not_flagged_as_past_their_close_date(dirty_report):
    # Deal 509 is closedwon with a past close date. That is normal, not a fault.
    # 502 is the open one with a past date, asserted alongside so this cannot
    # pass by the check having broken and returned nothing at all.
    flagged = ids_for(dirty_report, "deals.past_close_date")
    assert "502" in flagged
    assert "509" not in flagged


# -- 2. every check can pass -----------------------------------------------

def test_no_check_fails_on_a_clean_portal(clean_report):
    failing = [(r.check_id, [f.title for f in r.findings])
               for r in clean_report.failures]
    assert failing == []


def test_every_check_that_ran_on_the_clean_portal_actually_passed(clean_report):
    """Exact counts, not a floor.

    A floor of ">= 15" would let ten checks silently degrade to NOT_RUN, which
    is the failure mode this whole package is organised around.
    """
    statuses = {r.check_id: r.status for r in clean_report.results}
    passing = sorted(cid for cid, st in statuses.items() if st == Status.PASS)
    skipped = sorted(cid for cid, st in statuses.items() if st == Status.NOT_RUN)
    assert len(passing) == 25, statuses
    # The clean portal has no custom company or deal properties, so those two
    # and only those two are legitimately not run.
    assert skipped == ["schema.unused_custom_properties.companies",
                       "schema.unused_custom_properties.deals"]
    assert not [cid for cid, st in statuses.items()
                if st in (Status.FAIL, Status.ERROR)]


def test_every_planted_check_id_can_also_pass(clean_report):
    """Each rule is proved capable of both outcomes, not just of firing."""
    for check_id in PLANTED:
        result = result_for(clean_report, check_id)
        assert result.status == Status.PASS, (
            "%s could not reach PASS on a clean portal: %s / %s"
            % (check_id, result.status, result.reason))


# -- 3. nothing passes vacuously -------------------------------------------

def test_a_check_that_examined_nothing_reports_not_run_with_a_reason():
    report = audit(contacts_only_portal())
    deal_results = [r for r in report.results if r.object_type == "deals"]
    assert deal_results, "expected the deal checks to appear in the report"
    for result in deal_results:
        assert result.status == Status.NOT_RUN
        assert result.reason, "%s skipped without saying why" % result.check_id


def test_contacts_still_audit_on_a_portal_with_no_other_objects():
    report = audit(contacts_only_portal())
    contact_passes = [r for r in report.results
                      if r.object_type == "contacts" and r.status == Status.PASS]
    assert contact_passes, "a contacts-only portal should still get a real audit"


def test_passed_refuses_to_be_constructed_with_zero_records():
    from hubspot_audit.models import passed
    from hubspot_audit.checks.contacts import MissingEmail
    with pytest.raises(ValueError):
        passed(MissingEmail(), 0)


def test_every_check_declares_the_properties_it_reads():
    """A check reading a property it never requested would see a blank field
    on every record and pass silently. This is the guard against that."""
    with FakePortal(clean_portal()) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        profile = discover(client)
    for check in build_checks(profile):
        check.requires(profile)  # resolves lazily-computed property lists
        declared = set(getattr(check, "needs_properties", ()) or ())
        for stream_props in getattr(check, "stream_properties", {}).values():
            declared.update(stream_props)
        associations = set(getattr(check, "needs_associations", ()) or ())
        for stream_assoc in getattr(check, "stream_associations", {}).values():
            associations.update(stream_assoc)
        assert declared or associations or check.check_id.startswith("schema."), (
            "%s declares neither properties nor associations, so it cannot be "
            "reading anything" % check.check_id)


def test_the_runner_requests_exactly_the_declared_properties():
    state = clean_portal()
    audit(state)
    contact_requests = [q for path, q in state.request_log
                        if path == "/crm/v3/objects/contacts"]
    assert contact_requests
    asked = set(contact_requests[0]["properties"][0].split(","))

    # Build the expected set from what the checks actually declare, rather than
    # from a hardcoded list that could drift away from the code it guards.
    with FakePortal(clean_portal()) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        profile = discover(client)
    expected = set()
    for check in build_checks(profile):
        if check.requires(profile):
            continue
        if hasattr(check, "stream_properties"):
            expected.update(check.stream_properties.get("contacts", ()))
            expected.update(check.stream_optional_properties.get("contacts", ()))
        if check.object_type == "contacts":
            expected.update(check.needs_properties)
            expected.update(check.optional_properties)
    assert asked == expected, "requested %s, declared %s" % (
        sorted(asked), sorted(expected))

    associations = set(contact_requests[0]["associations"][0].split(","))
    assert "companies" in associations


def test_each_object_type_is_paged_exactly_once_for_the_whole_suite():
    """Twenty-odd checks must not mean twenty-odd scans."""
    state = clean_portal()
    audit(state)
    for object_type in ("contacts", "companies", "deals"):
        calls = [p for p, _q in state.request_log
                 if p == "/crm/v3/objects/%s" % object_type]
        assert len(calls) == 1, "%s was paged %d times" % (object_type, len(calls))


def test_requires_is_called_exactly_once_per_check():
    """Regression guard for a bug that cost real debugging time.

    Several checks initialise counters inside requires(). The runner used to
    call it a second time after observation, which reset those counters and
    made a clean portal report every custom property as unused. Nothing about
    that failure pointed at the double call, so it is pinned here.
    """
    state = clean_portal()
    calls = []

    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        profile = discover(client)
        checks = build_checks(profile)
        for check in checks:
            original = check.requires

            def counting(prof, _check=check, _original=original):
                calls.append(_check.check_id)
                return _original(prof)

            check.requires = counting
        run_audit(client, profile=profile, checks=checks)

    duplicated = sorted({cid for cid in calls if calls.count(cid) > 1})
    assert duplicated == [], "requires() called more than once for: %s" % duplicated


def test_a_portal_with_custom_lifecycle_stages_skips_rather_than_guessing():
    """The guard on the lifecycle check, which nothing used to exercise.

    Replacing its condition with `if False:` left the whole suite green, which
    means the most interesting check in the package could have been silently
    guessing at somebody's revenue data.
    """
    report = audit(custom_lifecycle_portal())
    result = result_for(report, "crossobject.lifecycle_contradicts_deal")
    assert result.status == Status.NOT_RUN
    assert "custom lifecycle stages" in result.reason
    assert "customer" in result.reason


def test_a_portal_with_no_pipeline_stages_skips_the_deal_stage_checks():
    report = audit(no_pipeline_portal())
    for check_id in ("deals.past_close_date", "deals.no_amount",
                     "deals.no_close_date", "deals.stale"):
        result = result_for(report, check_id)
        assert result.status == Status.NOT_RUN, check_id
        assert "cannot be told apart" in result.reason
    # A check that does not depend on stages still runs.
    assert result_for(report, "deals.no_owner").status == Status.FAIL
