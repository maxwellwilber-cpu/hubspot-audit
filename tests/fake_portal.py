"""A fake HubSpot portal, served over real HTTP on localhost.

Why a real socket instead of monkeypatching urlopen: the things that break a
CRM integration are status codes, headers, cursors and retry behaviour, and you
cannot test any of those against a stubbed function. This serves the documented
v3 contract so the client under test does real HTTP against real responses.

Response shapes follow HubSpot's published v3 documentation:
  objects:    {"results":[{"id","properties","createdAt","updatedAt","archived"}],
               "paging":{"next":{"after","link"}}}
  properties: {"results":[{"name","label","type","fieldType",...}]}
  429 body:   {"status","message","errorType":"RATE_LIMIT","policyName","correlationId"}

The properties shape is assembled from HubSpot's documented field list rather
than from a published literal example, so it is the thing in here most worth
re-checking the first time this runs against a live portal.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class RawBody:
    """Bytes served verbatim, for the non-JSON responses proxies return."""

    def __init__(self, payload, content_type="text/html"):
        self.payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.content_type = content_type


class PortalState:
    """Everything the fake portal will serve, plus the faults to inject."""

    def __init__(self):
        self.objects = {}       # "contacts" -> [record dict, ...]
        self.properties = {}    # "contacts" -> [property dict, ...]
        self.pipelines = []
        self.owners = []
        self.page_size_cap = 100

        # Fault injection. Each is consumed as it fires.
        self.fail_next = []     # list of (status, body) to serve then discard
        self.status_for_path = {}   # exact path prefix -> (status, body)
        self.rate_limit_headers = {
            "X-HubSpot-RateLimit-Max": "100",
            "X-HubSpot-RateLimit-Remaining": "99",
            "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
            "X-HubSpot-RateLimit-Daily": "250000",
            "X-HubSpot-RateLimit-Daily-Remaining": "249999",
        }
        self.require_token = "test-token"
        self.request_log = []   # (path, query) for assertions
        self.method_log = []    # every HTTP method the portal was asked for

        # When True the portal honours the `properties` and `associations`
        # query parameters the way the real API does: anything not asked for is
        # simply absent from the response. This is what catches a check that
        # reads a property it never declared.
        self.strict_params = True

        # Cursor games. When True, the portal hands back a cursor it has
        # already issued, which is the shape of the infinite-loop bug.
        self.repeat_cursor = False


def contact(record_id, **props):
    """Build a contacts record in the documented envelope."""
    return _record(record_id, props)


def company(record_id, **props):
    return _record(record_id, props)


def deal(record_id, **props):
    return _record(record_id, props)


def _record(record_id, props):
    created = props.pop("_createdAt", "2024-01-01T00:00:00.000Z")
    updated = props.pop("_updatedAt", "2024-06-01T00:00:00.000Z")
    archived = props.pop("_archived", False)
    associations = props.pop("_associations", None)
    body = {
        "id": str(record_id),
        "properties": {k: v for k, v in props.items()},
        "createdAt": created,
        "updatedAt": updated,
        "archived": archived,
    }
    # hs_object_id is always present on a real record and some checks lean on
    # it, so mirror HubSpot and include it automatically.
    body["properties"].setdefault("hs_object_id", str(record_id))
    if associations:
        body["associations"] = associations
    return body


def prop(name, type_="string", field_type="text", **extra):
    """Build a property definition in the documented shape."""
    body = {
        "name": name,
        "label": extra.pop("label", name.replace("_", " ").title()),
        "type": type_,
        "fieldType": field_type,
        "description": extra.pop("description", ""),
        "groupName": extra.pop("groupName", "contactinformation"),
        "options": extra.pop("options", []),
        "displayOrder": extra.pop("displayOrder", -1),
        "hidden": extra.pop("hidden", False),
        "hubspotDefined": extra.pop("hubspotDefined", True),
        "calculated": extra.pop("calculated", False),
        "externalOptions": extra.pop("externalOptions", False),
        "archived": extra.pop("archived", False),
        "modificationMetadata": extra.pop("modificationMetadata", {
            "archivable": True, "readable": True, "updateable": True,
        }),
    }
    body.update(extra)
    return body


def associations_to(object_type, *ids):
    """The associations envelope the v3 objects endpoint returns."""
    return {
        object_type: {
            "results": [{"id": str(i), "type": "x_to_y"} for i in ids]
        }
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self):
        return self.server.state

    def log_message(self, *args):
        pass  # keep the test output clean

    # Every method is routed through one handler so that a non-GET request is
    # RECORDED rather than silently answered with BaseHTTPRequestHandler's
    # default 501. tests/test_readonly.py asserts the method log holds only
    # GETs after a full audit, which is the runtime half of the read-only
    # guarantee -- the static half cannot see a urlopen(data=...) call.
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        self.state.request_log.append((path, query))
        self.state.method_log.append(self.command)

        if self.command != "GET":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            return self._send(405, {"status": "error",
                                    "message": "this portal is read-only"})

        if self.state.fail_next:
            status, body = self.state.fail_next.pop(0)
            return self._send(status, body)

        for prefix, (status, body) in self.state.status_for_path.items():
            if path.startswith(prefix):
                return self._send(status, body)

        auth = self.headers.get("Authorization") or ""
        if self.state.require_token and auth != "Bearer " + self.state.require_token:
            return self._send(401, {
                "status": "error",
                "message": "Authentication credentials not found. This API supports OAuth 2.0.",
                "correlationId": "00000000-0000-0000-0000-000000000001",
                "category": "INVALID_AUTHENTICATION",
            })

        if path.startswith("/crm/v3/properties/"):
            object_type = path.rsplit("/", 1)[-1]
            return self._send(200, {"results": self.state.properties.get(object_type, [])})

        if path == "/crm/v3/pipelines/deals":
            return self._send(200, {"results": self.state.pipelines})

        if path.startswith("/crm/v3/owners"):
            return self._send(200, {"results": self.state.owners})

        if path.startswith("/crm/v3/objects/"):
            object_type = path.split("/crm/v3/objects/", 1)[1].strip("/")
            if object_type not in self.state.objects:
                return self._send(404, {
                    "status": "error",
                    "message": "Unable to infer object type from: %s" % object_type,
                    "correlationId": "00000000-0000-0000-0000-000000000002",
                })
            return self._send_page(object_type, query)

        return self._send(404, {"status": "error", "message": "no route for %s" % path})

    def _send_page(self, object_type, query):
        records = self.state.objects[object_type]
        limit = min(int(query.get("limit", ["100"])[0]), self.state.page_size_cap)
        after = query.get("after", [None])[0]

        start = 0
        if after is not None:
            # HubSpot's cursor is opaque; ours is the index, which behaves the
            # same from the client's side.
            try:
                start = int(after)
            except ValueError:
                return self._send(400, {"status": "error", "message": "bad cursor"})

        page = records[start:start + limit]
        if self.state.strict_params:
            page = [self._project(r, query) for r in page]
        body = {"results": page}
        next_index = start + limit
        if next_index < len(records):
            cursor = str(start if self.state.repeat_cursor else next_index)
            body["paging"] = {"next": {
                "after": cursor,
                "link": "/crm/v3/objects/%s" % object_type,
            }}
        self._send(200, body)

    # HubSpot returns only the properties you ask for, plus a small default
    # set, and only the association types you ask for. A fake that returns
    # everything regardless hides the bug where a check reads a field the
    # runner never requested -- which on a real portal reads as empty on every
    # record and passes silently.
    DEFAULT_PROPERTIES = ("hs_object_id", "createdate", "lastmodifieddate")

    def _project(self, record, query):
        requested = set(self.DEFAULT_PROPERTIES)
        for value in query.get("properties", []):
            requested.update(p.strip() for p in value.split(",") if p.strip())
        projected = dict(record)
        projected["properties"] = {
            k: v for k, v in (record.get("properties") or {}).items()
            if k in requested
        }
        association_types = set()
        for value in query.get("associations", []):
            association_types.update(a.strip() for a in value.split(",") if a.strip())
        if "associations" in record:
            kept = {k: v for k, v in record["associations"].items()
                    if k in association_types}
            if kept:
                projected["associations"] = kept
            else:
                projected.pop("associations", None)
        return projected

    def _send(self, status, body):
        # A body wrapped in RawBody is written through untouched, so tests can
        # serve the HTML error pages that proxies and gateways return.
        if isinstance(body, RawBody):
            payload = body.payload
            content_type = body.content_type
        else:
            payload = json.dumps(body).encode("utf-8")
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in self.state.rate_limit_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


class FakePortal:
    """Context manager that runs the fake portal and hands back its base URL."""

    def __init__(self, state=None):
        self.state = state or PortalState()
        self._server = None
        self._thread = None

    @property
    def base_url(self):
        host, port = self._server.server_address
        return "http://127.0.0.1:%d" % port

    def __enter__(self):
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self.state
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


def rate_limit_body(policy="SECONDLY"):
    """The 429 body HubSpot documents."""
    return {
        "status": "error",
        "message": "You have reached your %s limit." % policy.lower(),
        "errorType": "RATE_LIMIT",
        "correlationId": "00000000-0000-0000-0000-000000000003",
        "policyName": policy,
        "requestId": "00000000-0000-0000-0000-000000000004",
    }


def missing_scope_body():
    return {
        "status": "error",
        "message": "This app hasn't been granted all required scopes to make this call. "
                   "Read more about scopes here: https://developers.hubspot.com/scopes",
        "correlationId": "00000000-0000-0000-0000-000000000005",
        "category": "MISSING_SCOPES",
    }
