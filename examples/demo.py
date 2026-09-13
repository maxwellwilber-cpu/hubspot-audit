"""Run a full audit against a synthetic portal. No HubSpot account needed.

    python3 examples/demo.py

This spins up a local HTTP server that speaks the HubSpot CRM v3 contract,
fills it with a deliberately messy portal, and runs the real audit against it
over real HTTP. Nothing here is mocked at the function level -- the client
under test makes actual requests and parses actual responses.

The messy portal is the same one the test suite uses. One fake API, one set of
planted defects, so the demo cannot drift away from what is actually tested.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fake_portal import FakePortal              # noqa: E402
from planted import PLANTED, dirty_portal       # noqa: E402

from hubspot_audit.audit import run_audit       # noqa: E402
from hubspot_audit.client import HubSpotClient  # noqa: E402
from hubspot_audit.report import render_markdown, render_terminal  # noqa: E402


def main():
    with FakePortal(dirty_portal()) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url)
        report = run_audit(client)

    print(render_terminal(report))

    # Score by the IDS each check reported, not merely by whether it fired. A
    # check that flagged every record in the portal would otherwise count as a
    # catch.
    found = {}
    for finding in report.findings:
        found.setdefault(finding.check_id, set()).update(finding.record_ids)

    missed, wrong = [], []
    for check_id, expected in sorted(PLANTED.items()):
        got = found.get(check_id)
        if not got:
            missed.append(check_id)
        elif got != set(expected):
            wrong.append("%s flagged %s, expected %s"
                         % (check_id, sorted(got), sorted(expected)))

    print()
    print("Planted defects: %d" % len(PLANTED))
    print("Caught exactly:  %d" % (len(PLANTED) - len(missed) - len(wrong)))
    print("Missed:          %d%s" % (len(missed), (" -> " + ", ".join(missed)) if missed else ""))
    if wrong:
        print("Wrong records:   %d" % len(wrong))
        for line in wrong:
            print("   " + line)
    print()
    print("Each defect is planted on purpose in tests/planted.py and mapped to "
          "the check that should catch it AND the record ids it should report. "
          "Worth being clear about what that does and does not prove: the "
          "expectation list is written alongside the code, so it measures "
          "whether the rules do what they claim, not whether the rules are the "
          "right rules.")

    out = os.path.join(ROOT, "demo_report.md")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
    print("Client-facing version written to %s" % out)

    return 0 if not (missed or wrong) else 1


if __name__ == "__main__":
    raise SystemExit(main())
