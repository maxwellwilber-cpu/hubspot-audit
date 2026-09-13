"""Portal discovery: it must degrade with a reason instead of assuming."""

from hubspot_audit.client import HubSpotClient
from hubspot_audit.schema import discover
from fake_portal import (
    FakePortal, PortalState, missing_scope_body, prop,
)


def client_for(portal):
    return HubSpotClient("test-token", base_url=portal.base_url, sleep=lambda _s: None)


def full_state():
    state = PortalState()
    state.objects = {"contacts": [], "companies": [], "deals": []}
    state.properties = {
        "contacts": [prop("email"), prop("firstname"), prop("lifecyclestage",
                     "enumeration", "select",
                     options=[{"value": "lead", "label": "Lead"},
                              {"value": "customer", "label": "Customer"}])],
        "companies": [prop("name"), prop("domain")],
        "deals": [prop("dealstage"), prop("amount", "number", "number")],
    }
    state.pipelines = [{
        "id": "default", "label": "Sales Pipeline",
        "stages": [
            {"id": "appointmentscheduled", "label": "Appointment",
             "metadata": {"isClosed": "false", "probability": "0.2"}, "displayOrder": 0},
            {"id": "closedwon", "label": "Closed Won",
             "metadata": {"isClosed": "true", "probability": "1.0"}, "displayOrder": 1},
            {"id": "closedlost", "label": "Closed Lost",
             "metadata": {"isClosed": "true", "probability": "0.0"}, "displayOrder": 2},
        ],
    }]
    state.owners = [{"id": "77", "email": "rep@x.test"}]
    return state


def test_discovers_objects_properties_pipelines_and_owners():
    with FakePortal(full_state()) as portal:
        profile = discover(client_for(portal))
    assert profile.available("contacts")
    assert profile.schema("contacts").has("email", "firstname")
    assert profile.open_stage_ids == {"appointmentscheduled"}
    assert profile.closed_won_stage_ids == {"closedwon"}
    assert profile.owner_ids == {"77"}


def test_closed_lost_is_not_counted_as_won():
    with FakePortal(full_state()) as portal:
        profile = discover(client_for(portal))
    assert "closedlost" not in profile.closed_won_stage_ids


def test_a_portal_with_no_deals_object_is_recorded_not_raised():
    state = full_state()
    del state.objects["deals"]
    state.status_for_path["/crm/v3/properties/deals"] = (
        404, {"status": "error", "message": "no such object"})
    with FakePortal(state) as portal:
        profile = discover(client_for(portal))
    assert profile.available("contacts")
    assert not profile.available("deals")
    assert "does not have the deals object" in profile.schema("deals").reason


def test_a_missing_scope_is_recorded_with_the_scope_name():
    state = full_state()
    state.status_for_path["/crm/v3/properties/companies"] = (403, missing_scope_body())
    with FakePortal(state) as portal:
        profile = discover(client_for(portal))
    assert not profile.available("companies")
    assert "crm.objects.companies.read" in profile.schema("companies").reason


def test_enum_options_are_read_from_the_portal_not_assumed():
    with FakePortal(full_state()) as portal:
        profile = discover(client_for(portal))
    assert profile.schema("contacts").enum_options("lifecyclestage") == ["lead", "customer"]


def test_missing_reports_exactly_which_properties_are_absent():
    with FakePortal(full_state()) as portal:
        profile = discover(client_for(portal))
    assert profile.schema("contacts").missing("email", "nope", "alsonope") == ["nope", "alsonope"]


def test_owners_endpoint_failing_does_not_sink_discovery():
    state = full_state()
    state.status_for_path["/crm/v3/owners"] = (403, missing_scope_body())
    with FakePortal(state) as portal:
        profile = discover(client_for(portal))
    assert profile.owner_ids == set()
    assert profile.available("contacts")


def test_a_rejected_token_raises_instead_of_producing_an_empty_report():
    """The worst possible output is a report where every check was skipped.

    Someone handed that would read 'no findings' as good news. A 401 has to
    stop the run, not degrade it, so discovering nothing and discovering
    nothing wrong can never look the same.
    """
    import pytest
    from hubspot_audit.errors import AuthError

    state = full_state()
    state.require_token = "the-real-token"
    with FakePortal(state) as portal:
        with pytest.raises(AuthError):
            discover(client_for(portal))


def test_a_portal_where_no_object_can_be_read_raises_rather_than_reporting():
    import pytest
    from hubspot_audit.errors import NoAuditableObjects

    state = full_state()
    for object_type in ("contacts", "companies", "deals"):
        state.status_for_path["/crm/v3/properties/%s" % object_type] = (
            403, missing_scope_body())
    with FakePortal(state) as portal:
        with pytest.raises(NoAuditableObjects) as caught:
            discover(client_for(portal))
    # The message has to name what went wrong for each object, not just fail.
    assert "contacts" in str(caught.value)
    assert "scope" in str(caught.value)


def test_one_unreadable_object_still_allows_an_audit_of_the_rest():
    state = full_state()
    state.status_for_path["/crm/v3/properties/deals"] = (403, missing_scope_body())
    with FakePortal(state) as portal:
        profile = discover(client_for(portal))
    assert profile.available("contacts")
    assert not profile.available("deals")
