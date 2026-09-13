"""Command line entry point.

    python3 -m hubspot_audit --token pat-na1-... --markdown report.md --csv fix.csv

The token can also come from the HUBSPOT_TOKEN environment variable, which is
the better habit: a token pasted on a command line ends up in shell history.
"""

import argparse
import os
import sys

from .audit import run_audit
from .client import HubSpotClient
from .errors import (
    AuthError, HubSpotError, NoAuditableObjects, RateLimitError, ScopeError,
)
from .report import render_csv, render_json, render_markdown, render_terminal
from .schema import discover


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hubspot-audit",
        description="Read-only CRM hygiene audit for a HubSpot portal. "
                    "This tool never writes to your CRM.")
    parser.add_argument("--token", default=os.environ.get("HUBSPOT_TOKEN"),
                        help="HubSpot private app token. Defaults to $HUBSPOT_TOKEN. "
                             "Prefer the environment variable so the token stays "
                             "out of your shell history.")
    parser.add_argument("--base-url", default="https://api.hubapi.com",
                        help=argparse.SUPPRESS)  # tests point this at a local server
    parser.add_argument("--markdown", metavar="PATH",
                        help="write the client-facing report to this file")
    parser.add_argument("--csv", metavar="PATH",
                        help="write one row per affected record to this file")
    parser.add_argument("--json", metavar="PATH",
                        help="write the full machine-readable result to this file")
    parser.add_argument("--max-records", type=int, default=None,
                        help="stop after this many records per object type. Use for "
                             "a fast sample of a very large portal. Every output "
                             "carries a partial-scan banner, and cross-object "
                             "checks are skipped because a truncated stream would "
                             "make them invent findings.")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress the terminal summary")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.max_records is not None and args.max_records < 1:
        sys.stderr.write("--max-records must be 1 or more. Omit it to scan "
                         "the whole portal.\n")
        return 2

    if not args.token:
        sys.stderr.write(
            "No token. Set HUBSPOT_TOKEN or pass --token.\n"
            "Create one at Settings > Integrations > Private Apps, with read "
            "scopes only. See README.md for the exact scope list.\n")
        return 2

    try:
        client = HubSpotClient(args.token, base_url=args.base_url)
        profile = discover(client)
        report = run_audit(client, profile=profile, max_records=args.max_records)
    except ScopeError as exc:
        sys.stderr.write("%s\n" % exc)
        sys.stderr.write("Add the missing read scope to the private app, then "
                         "regenerate the token.\n")
        return 3
    except AuthError as exc:
        sys.stderr.write("%s\n" % exc)
        return 3
    except NoAuditableObjects as exc:
        sys.stderr.write("%s\n" % exc)
        sys.stderr.write("Nothing was read, so there is nothing to report. "
                         "This is not a clean bill of health.\n")
        return 3
    except RateLimitError as exc:
        sys.stderr.write("%s\n" % exc)
        if exc.is_daily:
            sys.stderr.write("This is the daily quota, not a burst limit. "
                             "Try again tomorrow.\n")
        return 4
    except HubSpotError as exc:
        sys.stderr.write("HubSpot request failed: %s\n" % exc)
        return 4

    if not args.quiet:
        sys.stdout.write(render_terminal(report) + "\n")

    for path, renderer in ((args.markdown, render_markdown),
                           (args.csv, render_csv),
                           (args.json, render_json)):
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(renderer(report))
            if not args.quiet:
                sys.stdout.write("Wrote %s\n" % path)

    # Exit 1 when something was found, so this can gate a CI job or a cron.
    # Not an error: a failing audit is the tool working.
    return 1 if report.failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
