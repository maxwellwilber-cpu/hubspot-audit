"""The command line, including the exit codes a cron or CI job would read."""

import json
import os

from hubspot_audit.cli import main
from fake_portal import FakePortal, missing_scope_body
from planted import clean_portal, dirty_portal


def run(state, argv):
    with FakePortal(state) as portal:
        return main(["--base-url", portal.base_url, "--token", "test-token"] + argv)


def test_exit_code_1_when_findings_exist(capsys):
    assert run(dirty_portal(), ["--quiet"]) == 1


def test_exit_code_0_on_a_clean_portal(capsys):
    assert run(clean_portal(), ["--quiet"]) == 0


def test_missing_token_is_refused_before_any_network_call(capsys, monkeypatch):
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    assert main([]) == 2
    assert "Private Apps" in capsys.readouterr().err


def test_bad_token_exits_3_and_does_not_render_a_report(capsys):
    state = dirty_portal()
    state.require_token = "the-real-token"
    with FakePortal(state) as portal:
        code = main(["--base-url", portal.base_url, "--token", "wrong"])
    captured = capsys.readouterr()
    assert code == 3
    assert "401" in captured.err
    assert "HUBSPOT CRM AUDIT" not in captured.out


def test_missing_scopes_everywhere_exits_3_with_a_warning_not_a_clean_report(capsys):
    state = dirty_portal()
    for object_type in ("contacts", "companies", "deals"):
        state.status_for_path["/crm/v3/properties/%s" % object_type] = (
            403, missing_scope_body())
    with FakePortal(state) as portal:
        code = main(["--base-url", portal.base_url, "--token", "test-token"])
    captured = capsys.readouterr()
    assert code == 3
    assert "not a clean bill of health" in captured.err


def test_writes_all_three_output_files(tmp_path, capsys):
    markdown = tmp_path / "report.md"
    csv_path = tmp_path / "fix.csv"
    json_path = tmp_path / "audit.json"
    run(dirty_portal(), ["--quiet", "--markdown", str(markdown),
                         "--csv", str(csv_path), "--json", str(json_path)])
    assert markdown.read_text().startswith("# HubSpot CRM audit")
    assert csv_path.read_text().splitlines()[0].startswith("object_type,record_id")
    payload = json.loads(json_path.read_text())
    assert payload["summary"]["failed"] > 0


def test_token_can_come_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("HUBSPOT_TOKEN", "test-token")
    with FakePortal(clean_portal()) as portal:
        code = main(["--base-url", portal.base_url, "--quiet"])
    assert code == 0
