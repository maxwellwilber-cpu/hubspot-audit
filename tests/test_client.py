"""The HTTP client: it has to survive everything HubSpot can return."""

import pytest

from hubspot_audit.client import HubSpotClient, RateLimiter
from hubspot_audit.errors import (
    AuthError, NotFoundError, RateLimitError, ScopeError, ServerError,
)
from fake_portal import (
    FakePortal, PortalState, RawBody, contact, missing_scope_body, rate_limit_body,
)


def portal_with(records, object_type="contacts"):
    state = PortalState()
    state.objects[object_type] = records
    return state


def client_for(portal, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)  # never actually sleep in tests
    return HubSpotClient("test-token", base_url=portal.base_url, **kwargs)


def test_single_page_returns_every_record():
    state = portal_with([contact(i, email="a%d@x.test" % i) for i in range(10)])
    with FakePortal(state) as portal:
        got = list(client_for(portal).paginate("/crm/v3/objects/contacts"))
    assert [r["id"] for r in got] == [str(i) for i in range(10)]


def test_pagination_follows_the_cursor_across_many_pages():
    state = portal_with([contact(i) for i in range(250)])
    with FakePortal(state) as portal:
        client = client_for(portal)
        got = list(client.paginate("/crm/v3/objects/contacts", page_size=100))
    assert len(got) == 250
    assert [r["id"] for r in got] == [str(i) for i in range(250)]
    # 250 records at 100 a page is three requests, not four.
    assert client.request_count == 3


def test_page_size_is_clamped_to_the_api_maximum():
    state = portal_with([contact(i) for i in range(10)])
    with FakePortal(state) as portal:
        list(client_for(portal).paginate("/crm/v3/objects/contacts", page_size=5000))
    _path, query = state.request_log[0]
    assert query["limit"] == ["100"]


def test_a_partial_final_page_ends_cleanly():
    state = portal_with([contact(i) for i in range(105)])
    with FakePortal(state) as portal:
        got = list(client_for(portal).paginate("/crm/v3/objects/contacts"))
    assert len(got) == 105


def test_empty_portal_yields_nothing_and_does_not_hang():
    state = portal_with([])
    with FakePortal(state) as portal:
        got = list(client_for(portal).paginate("/crm/v3/objects/contacts"))
    assert got == []


def test_a_repeated_cursor_terminates_instead_of_looping_forever():
    state = portal_with([contact(i) for i in range(300)])
    state.repeat_cursor = True
    with FakePortal(state) as portal:
        client = client_for(portal)
        got = list(client.paginate("/crm/v3/objects/contacts"))
    # Exact, not a bound: a bound this loose is also satisfied by pagination
    # returning nothing at all. Two requests, the second repeating page one.
    assert client.request_count == 2
    assert len(got) == 200


def test_401_raises_auth_error_with_a_useful_message():
    state = portal_with([])
    state.require_token = "the-real-token"
    with FakePortal(state) as portal:
        with pytest.raises(AuthError) as caught:
            list(client_for(portal).paginate("/crm/v3/objects/contacts"))
    assert "401" in str(caught.value)
    assert caught.value.correlation_id


def test_403_raises_scope_error_naming_the_scope_we_needed():
    state = portal_with([])
    state.status_for_path["/crm/v3/objects/contacts"] = (403, missing_scope_body())
    with FakePortal(state) as portal:
        with pytest.raises(ScopeError) as caught:
            list(client_for(portal).paginate(
                "/crm/v3/objects/contacts",
                required_scope="crm.objects.contacts.read"))
    assert caught.value.required_scope == "crm.objects.contacts.read"
    assert "crm.objects.contacts.read" in str(caught.value)


def test_secondly_rate_limit_is_retried_and_then_succeeds():
    state = portal_with([contact(1)])
    state.fail_next = [(429, rate_limit_body("SECONDLY")),
                       (429, rate_limit_body("SECONDLY"))]
    slept = []
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=slept.append)
        got = list(client.paginate("/crm/v3/objects/contacts"))
    assert len(got) == 1
    # Exact values, because "the second is larger than the first" is equally
    # true of [1, 2] -- which is what you get if the client ignores HubSpot's
    # own interval header and falls back to its 1-second default.
    assert slept == [10.0, 20.0]    # 10000ms interval, doubling per attempt


def test_daily_rate_limit_fails_immediately_instead_of_sleeping():
    state = portal_with([contact(1)])
    state.fail_next = [(429, rate_limit_body("DAILY"))]
    slept = []
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=slept.append)
        with pytest.raises(RateLimitError) as caught:
            list(client.paginate("/crm/v3/objects/contacts"))
    assert caught.value.is_daily
    assert slept == []  # waiting would be pointless, so we do not


def test_retries_are_bounded_and_then_raise():
    state = portal_with([contact(1)])
    state.fail_next = [(429, rate_limit_body("SECONDLY"))] * 20
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None, max_retries=3)
        with pytest.raises(RateLimitError):
            list(client.paginate("/crm/v3/objects/contacts"))
    assert client.request_count == 4  # first try plus three retries


def test_500_is_retried_then_gives_up():
    state = portal_with([contact(1)])
    state.fail_next = [(500, {"status": "error", "message": "boom"})] * 20
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None, max_retries=2)
        with pytest.raises(ServerError):
            list(client.paginate("/crm/v3/objects/contacts"))
    assert client.request_count == 3  # first try plus two retries


def test_500_that_recovers_is_transparent_to_the_caller():
    state = portal_with([contact(1)])
    state.fail_next = [(500, {"status": "error", "message": "boom"})]
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None)
        got = list(client.paginate("/crm/v3/objects/contacts"))
    assert len(got) == 1


def test_unknown_object_type_raises_not_found():
    state = portal_with([])
    with FakePortal(state) as portal:
        with pytest.raises(NotFoundError):
            list(client_for(portal).paginate("/crm/v3/objects/widgets"))


def test_empty_token_is_rejected_before_any_network_call():
    with pytest.raises(AuthError):
        HubSpotClient("   ")


def test_non_json_error_body_does_not_crash_the_parser():
    state = portal_with([])
    state.status_for_path["/crm/v3/objects/contacts"] = (
        502, RawBody("<html><body>502 Bad Gateway</body></html>"))
    with FakePortal(state) as portal:
        client = HubSpotClient("test-token", base_url=portal.base_url,
                               sleep=lambda _s: None, max_retries=0)
        with pytest.raises(ServerError) as caught:
            list(client.paginate("/crm/v3/objects/contacts"))
    # The HTML body has to survive into the message, or the caller is told
    # nothing about what the proxy actually said.
    assert "502 Bad Gateway" in str(caught.value)


# -- the limiter itself ----------------------------------------------------

def test_limiter_paces_once_the_window_is_full():
    slept = []
    now = [0.0]
    limiter = RateLimiter(max_requests=3, interval_ms=1000,
                          sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)),
                          clock=lambda: now[0])
    for _ in range(3):
        limiter.acquire()
    assert slept == []       # first three fit in the window
    limiter.acquire()
    assert len(slept) == 1   # the fourth had to wait
    assert slept[0] == pytest.approx(1.0, abs=0.01)


def test_limiter_believes_hubspots_headers_over_its_own_default():
    limiter = RateLimiter()
    limiter.observe_headers({"X-HubSpot-RateLimit-Max": "190",
                             "X-HubSpot-RateLimit-Interval-Milliseconds": "10000"})
    assert limiter.max_requests == 190


def test_limiter_ignores_garbage_headers():
    limiter = RateLimiter(max_requests=100)
    limiter.observe_headers({"X-HubSpot-RateLimit-Max": "not-a-number"})
    assert limiter.max_requests == 100
