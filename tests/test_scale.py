"""Scale: a real portal is not 14 records.

These tests drive the check pipeline directly rather than through the fake HTTP
server, because what is being measured is the audit itself. On a live portal
the wall clock is dominated by the rate limiter, and a tool that is slow on top
of that is unusable.

The thing being proved: duplicate detection is hash-keyed, not pairwise. A
naive implementation comparing every record to every other would be 1.25
billion comparisons at 50,000 contacts and would never finish.
"""

import time
import tracemalloc

import pytest

from hubspot_audit.audit import run_audit
from hubspot_audit.models import Status
from hubspot_audit.schema import ObjectSchema, Pipeline, PortalProfile
from planted import LIFECYCLE_OPTIONS, PIPELINE

CONTACT_COUNT = 50_000
DUPLICATE_EVERY = 50   # 1 in 50 contacts is a planted duplicate


def _properties(names, custom=()):
    out = []
    for name in names:
        options = LIFECYCLE_OPTIONS if name == "lifecyclestage" else []
        out.append({"name": name, "label": name, "type": "string",
                    "fieldType": "text", "options": options,
                    "hubspotDefined": True, "calculated": False,
                    "archived": False})
    for name in custom:
        out.append({"name": name, "label": name, "type": "string",
                    "fieldType": "text", "options": [],
                    "hubspotDefined": False, "calculated": False,
                    "archived": False})
    return out


def big_profile():
    return PortalProfile(
        schemas={
            "contacts": ObjectSchema("contacts", _properties(
                ["email", "firstname", "lastname", "phone",
                 "hubspot_owner_id", "lifecyclestage"], custom=["legacy_field"])),
            "companies": ObjectSchema("companies", _properties(
                ["name", "domain", "hubspot_owner_id"])),
            "deals": ObjectSchema("deals", _properties(
                ["dealname", "dealstage", "amount", "closedate",
                 "hubspot_owner_id", "hs_lastmodifieddate"])),
        },
        pipelines=[Pipeline(p) for p in PIPELINE],
        owners=[{"id": "77"}],
    )


def contact_stream(count):
    for i in range(count):
        # Every DUPLICATE_EVERY-th contact reuses the previous address, so the
        # duplicate rule has real work to do rather than finding nothing.
        email_index = i - 1 if i and i % DUPLICATE_EVERY == 0 else i
        yield {
            "id": str(i),
            "properties": {
                # The domain must follow email_index too, or the "duplicate" is a
                # different address and the check correctly finds nothing.
                "email": "person%d@example%d.test" % (email_index, email_index % 997),
                "firstname": "First%d" % (i % 5000),
                "lastname": "Last%d" % (i % 7000),
                "phone": "(555) %03d-%04d" % (i % 1000, i % 10000),
                "hubspot_owner_id": "77",
                "lifecyclestage": "lead",
                "legacy_field": "x",
            },
            "associations": {"companies": {"results": [{"id": str(i % 500)}]}},
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2026-09-01T00:00:00.000Z",
            "archived": False,
        }


def company_stream(count):
    for i in range(count):
        yield {
            "id": str(i),
            "properties": {"name": "Company %d" % i,
                           "domain": "company%d.test" % i,
                           "hubspot_owner_id": "77"},
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2026-09-01T00:00:00.000Z", "archived": False,
        }


def deal_stream(count):
    for i in range(count):
        yield {
            "id": str(i),
            "properties": {"dealname": "Deal %d" % i,
                           "dealstage": "appointmentscheduled",
                           "amount": "1000", "closedate": "2027-06-01T00:00:00.000Z",
                           "hubspot_owner_id": "77",
                           "hs_lastmodifieddate": "2026-09-01T00:00:00.000Z"},
            "associations": {
                "contacts": {"results": [{"id": str(i)}]},
                "companies": {"results": [{"id": str(i % 500)}]},
            },
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2026-09-01T00:00:00.000Z", "archived": False,
        }


class StreamingStubClient:
    """Yields generated records without HTTP, to time the audit and nothing else."""

    request_count = 0

    def __init__(self, counts):
        self.counts = counts

    def get(self, path, params=None, required_scope=None):  # pragma: no cover
        raise AssertionError("scale test should not hit the schema endpoints")

    def paginate(self, path, params=None, page_size=100, required_scope=None,
                 max_pages=None):
        object_type = path.rsplit("/", 1)[-1]
        count = self.counts.get(object_type, 0)
        if object_type == "contacts":
            return contact_stream(count)
        if object_type == "companies":
            return company_stream(count)
        if object_type == "deals":
            return deal_stream(count)
        return iter(())


@pytest.mark.parametrize("count", [CONTACT_COUNT])
def test_fifty_thousand_contacts_audits_in_reasonable_time_and_memory(count):
    client = StreamingStubClient({"contacts": count, "companies": 500, "deals": 2000})
    profile = big_profile()

    tracemalloc.start()
    started = time.monotonic()
    report = run_audit(client, profile=profile)
    elapsed = time.monotonic() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Generous ceilings. The point is to catch an accidental O(n^2), which
    # would not finish at all, not to benchmark the machine.
    assert elapsed < 60, "50k contacts took %.1fs" % elapsed
    assert peak < 600 * 1024 * 1024, "peak memory %.0fMB" % (peak / 1024 / 1024)

    duplicates = [r for r in report.results
                  if r.check_id == "contacts.duplicate_email"][0]
    assert duplicates.status == Status.FAIL
    # 1 in 50 contacts shares the previous contact's address, so each planted
    # collision involves 2 records.
    expected_groups = (count // DUPLICATE_EVERY) - 1
    assert duplicates.findings[0].evidence["duplicate_groups"] == expected_groups
    assert duplicates.records_examined == count


def test_duplicate_detection_scales_linearly_not_quadratically():
    """Ten times the records should cost roughly ten times the work, not a
    hundred times. A pairwise implementation fails this badly."""
    def timed(count):
        client = StreamingStubClient({"contacts": count, "companies": 0, "deals": 0})
        started = time.monotonic()
        run_audit(client, profile=big_profile())
        return time.monotonic() - started

    small = timed(2_000)
    large = timed(20_000)
    # Allow a wide margin for interpreter noise and fixed overhead. Quadratic
    # growth would put this ratio near 100.
    assert large / max(small, 1e-6) < 30, (
        "10x the records cost %.1fx the time, which looks super-linear"
        % (large / max(small, 1e-6)))


def test_max_records_stops_early_for_a_fast_sample():
    client = StreamingStubClient({"contacts": 10_000, "companies": 0, "deals": 0})
    report = run_audit(client, profile=big_profile(), max_records=500)
    contact_results = [r for r in report.results
                       if r.object_type == "contacts" and r.status != Status.NOT_RUN]
    assert contact_results
    assert all(r.records_examined == 500 for r in contact_results)
