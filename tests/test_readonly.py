"""Prove the read-only claim instead of asserting it.

The README tells a prospect this tool cannot modify their CRM. That is a
promise about someone else's business data, so it needs a test rather than a
sentence. This one reads the shipped package source and fails the build if any
write path appears.

It is deliberately blunt. A cleverer version that parsed intent would be easier
to fool; this one just refuses to let the strings exist.
"""

import ast
import pathlib
import re

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "hubspot_audit"

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def package_files():
    return sorted(p for p in PACKAGE.rglob("*.py"))


def test_the_package_ships_files_to_check():
    """Guard against this whole suite quietly passing because it found nothing."""
    files = package_files()
    assert len(files) >= 8, files


def test_no_http_write_verb_appears_anywhere_in_the_package():
    offenders = []
    for path in package_files():
        source = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            # Comments and docstring prose may legitimately mention the words.
            if stripped.startswith("#"):
                continue
            for verb in WRITE_METHODS:
                # Only flag the verb as a quoted string, which is the only way
                # it could reach urllib as a method.
                if re.search(r"""["']%s["']""" % verb, line):
                    offenders.append("%s:%d %s" % (path.name, line_no, stripped))
    assert offenders == [], (
        "a write verb appears in the package source:\n  " + "\n  ".join(offenders))


def test_every_request_is_constructed_with_method_get():
    """urllib.request.Request defaults to GET, but an explicit method= or a
    data= payload silently changes that. Both are checked."""
    problems = []
    for path in package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name != "Request":
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            method = kwargs.get("method")
            if method is None:
                problems.append("%s: Request built without an explicit method="
                                % path.name)
            elif not (isinstance(method, ast.Constant) and method.value == "GET"):
                problems.append("%s: Request built with a non-literal or "
                                "non-GET method" % path.name)
            if "data" in kwargs:
                problems.append("%s: Request built with data=, which makes it "
                                "a POST" % path.name)
    assert problems == [], "\n".join(problems)


def test_at_least_one_request_is_actually_constructed():
    """Otherwise the test above passes vacuously on a package that stopped
    making HTTP requests at all."""
    found = 0
    for path in package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Request":
                found += 1
    assert found >= 1


def test_no_call_can_send_a_request_body():
    """urllib.request.urlopen(url, data=...) is a POST that builds no Request
    node and contains no verb literal, so the checks above are blind to it.

    This was a working bypass: a function using it passed every read-only test
    while writing to a live portal. Any body-carrying call is now flagged
    wherever it appears, by keyword or by position.
    """
    problems = []
    body_senders = {"urlopen", "open", "request"}
    for path in package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in body_senders:
                continue
            if any(kw.arg == "data" for kw in node.keywords):
                problems.append("%s:%d %s(data=...) sends a request body"
                                % (path.name, node.lineno, name))
            # urlopen's second positional argument IS data.
            if name == "urlopen" and len(node.args) > 1:
                problems.append("%s:%d urlopen() with a second positional "
                                "argument sends a request body"
                                % (path.name, node.lineno))
    assert problems == [], "\n".join(problems)


def test_nothing_reassigns_a_requests_method_or_body_after_construction():
    """req.method = "POST" would pass every check that only reads the call."""
    problems = []
    for path in package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in {"method", "data"}:
                    problems.append("%s:%d assigns .%s on an object"
                                    % (path.name, node.lineno, target.attr))
    assert problems == [], "\n".join(problems)


def test_the_client_exposes_no_write_helpers():
    from hubspot_audit import client as client_module

    public = [name for name in dir(client_module.HubSpotClient)
              if not name.startswith("_")]
    # Substring, not exact equality. Exact matching let `create_contact` and
    # `upsert` through, both of which issued real writes.
    forbidden = ("post", "put", "patch", "delete", "create", "update", "write",
                 "archive", "merge", "upsert", "send", "batch")
    offenders = [name for name in public
                 if any(word in name.lower() for word in forbidden)]
    assert offenders == [], offenders


def test_a_full_audit_sends_nothing_but_get_requests():
    """The runtime half of the guarantee.

    Every other test in this file reads source code. This one runs a complete
    audit against a portal that records the HTTP method of every request it
    receives, and asserts nothing but GET ever arrives. Source analysis cannot
    see a dynamically constructed write; a socket can.
    """
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from fake_portal import FakePortal
    from planted import dirty_portal
    from hubspot_audit.audit import run_audit
    from hubspot_audit.client import HubSpotClient

    state = dirty_portal()
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        run_audit(client)

    assert state.method_log, "the audit made no requests at all"
    assert set(state.method_log) == {"GET"}, sorted(set(state.method_log))


def test_the_portal_would_actually_notice_a_write():
    """Otherwise the test above passes because the fake cannot see writes."""
    import sys
    import urllib.request

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from fake_portal import FakePortal
    from planted import dirty_portal

    state = dirty_portal()
    with FakePortal(state) as portal:
        request = urllib.request.Request(
            portal.base_url + "/crm/v3/objects/contacts", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(request, timeout=5)
        except Exception:
            pass  # a 405 is the expected outcome; what matters is the log
    assert "POST" in state.method_log


def test_no_module_imports_an_http_library_that_could_bypass_the_client():
    """requests, httpx and the official hubspot SDK would all be a way around
    the guarantees above. The package is standard library only by design."""
    banned = {"requests", "httpx", "aiohttp", "hubspot", "urllib3"}
    offenders = []
    for path in package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in banned:
                    offenders.append("%s imports %s" % (path.name, name))
    assert offenders == [], offenders
