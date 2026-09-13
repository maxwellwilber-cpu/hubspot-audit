"""Two portals: one deliberately broken, one deliberately clean.

Every defect in the dirty portal is planted on purpose and mapped to the check id that is supposed to catch it, so the test
suite asserts detection rather than asserting that the code does whatever it
currently does. The clean portal exists so every check is also proved capable
of returning PASS, which is what stops a check from being trivially always-red.
"""

from fake_portal import PortalState, associations_to, prop

PIPELINE = [{
    "id": "default",
    "label": "Sales Pipeline",
    "stages": [
        {"id": "appointmentscheduled", "label": "Appointment Scheduled",
         "metadata": {"isClosed": "false", "probability": "0.2"}, "displayOrder": 0},
        {"id": "contractsent", "label": "Contract Sent",
         "metadata": {"isClosed": "false", "probability": "0.8"}, "displayOrder": 1},
        {"id": "closedwon", "label": "Closed Won",
         "metadata": {"isClosed": "true", "probability": "1.0"}, "displayOrder": 2},
        {"id": "closedlost", "label": "Closed Lost",
         "metadata": {"isClosed": "true", "probability": "0.0"}, "displayOrder": 3},
    ],
}]

LIFECYCLE_OPTIONS = [
    {"value": "subscriber", "label": "Subscriber"},
    {"value": "lead", "label": "Lead"},
    {"value": "opportunity", "label": "Opportunity"},
    {"value": "customer", "label": "Customer"},
]

FUTURE = "2027-06-01T00:00:00.000Z"
PAST = "2024-02-01T00:00:00.000Z"
RECENT = "2026-09-01T00:00:00.000Z"
LONG_AGO = "2025-01-01T00:00:00.000Z"

# Which check each planted defect is meant to trip. The test suite reads this,
# so adding a defect without saying what it proves is not possible.
PLANTED = {
    "contacts.duplicate_email": ["1", "2", "15", "17", "16", "18"],
    "contacts.duplicate_name_phone": ["9", "10", "15", "16"],
    "contacts.missing_email": ["3"],
    "contacts.malformed_email": ["4"],
    "contacts.placeholder_email": ["5"],
    "contacts.role_account_email": ["6"],
    "contacts.no_owner": ["7"],
    "contacts.missing_lifecycle_stage": ["8"],
    "contacts.implausible_phone": ["11"],
    "contacts.no_company_association": ["13"],
    "companies.duplicate_domain": ["901", "902"],
    "companies.duplicate_name": ["903", "904"],
    "companies.missing_domain": ["905"],
    "companies.no_owner": ["906"],
    "deals.past_close_date": ["502"],
    "deals.no_owner": ["503"],
    "deals.no_amount": ["504"],
    "deals.no_close_date": ["505"],
    "deals.stale": ["506"],
    "crossobject.deal_without_contact": ["507"],
    "crossobject.deal_without_company": ["508"],
    "crossobject.lifecycle_contradicts_deal": ["14"],
    "crossobject.company_without_contacts": ["907"],
    # Portal-level rather than per-record, but planted just as deliberately.
    "contacts.phone_format_inconsistency": ["10", "12"],
    # Note: these are property NAMES, not record ids. The check says so in its
    # evidence block and the report renders them differently.
    "schema.unused_custom_properties.contacts": ["preferred_contact_method"],
}


def _properties():
    contact_props = [
        prop("email"), prop("firstname"), prop("lastname"), prop("phone"),
        prop("hubspot_owner_id"),
        prop("lifecyclestage", "enumeration", "select", options=LIFECYCLE_OPTIONS),
        prop("legacy_import_batch", hubspotDefined=False, groupName="custom"),
        prop("preferred_contact_method", hubspotDefined=False, groupName="custom"),
    ]
    company_props = [
        prop("name"), prop("domain"), prop("hubspot_owner_id"),
    ]
    deal_props = [
        prop("dealname"), prop("dealstage"), prop("amount", "number", "number"),
        prop("closedate", "datetime", "date"), prop("hubspot_owner_id"),
        prop("hs_lastmodifieddate", "datetime", "date"),
    ]
    return {"contacts": contact_props, "companies": company_props, "deals": deal_props}


def _contact(cid, email="", first="", last="", phone="", owner="77",
             lifecycle="lead", companies=(), updated=RECENT, **custom):
    props = {
        "email": email, "firstname": first, "lastname": last, "phone": phone,
        "hubspot_owner_id": owner, "lifecyclestage": lifecycle,
        "hs_object_id": str(cid),
    }
    props.update(custom)
    body = {
        "id": str(cid), "properties": props,
        "createdAt": "2024-01-01T00:00:00.000Z", "updatedAt": updated,
        "archived": False,
    }
    if companies:
        body["associations"] = associations_to("companies", *companies)
    return body


def _company(cid, name="", domain="", owner="77"):
    return {
        "id": str(cid),
        "properties": {"name": name, "domain": domain,
                       "hubspot_owner_id": owner, "hs_object_id": str(cid)},
        "createdAt": "2024-01-01T00:00:00.000Z",
        "updatedAt": RECENT, "archived": False,
    }


def _deal(did, stage="appointmentscheduled", amount="1000", close=FUTURE,
          owner="77", contacts=(), companies=(), updated=RECENT, name="Deal"):
    associations = {}
    if contacts:
        associations.update(associations_to("contacts", *contacts))
    if companies:
        associations.update(associations_to("companies", *companies))
    body = {
        "id": str(did),
        "properties": {"dealname": name, "dealstage": stage, "amount": amount,
                       "closedate": close, "hubspot_owner_id": owner,
                       "hs_lastmodifieddate": updated, "hs_object_id": str(did)},
        "createdAt": "2024-01-01T00:00:00.000Z",
        "updatedAt": updated, "archived": False,
    }
    if associations:
        body["associations"] = associations
    return body


def dirty_portal():
    """One planted defect per record, so a detection maps to exactly one cause."""
    state = PortalState()
    state.properties = _properties()
    state.pipelines = PIPELINE
    state.owners = [{"id": "77", "email": "rep@acme.test"}]

    # Every contact carries legacy_import_batch so only the second custom
    # property is dead; otherwise the unused-property check would have two
    # findings and the planted map would be ambiguous.
    common = {"legacy_import_batch": "2024-Q1"}

    state.objects["contacts"] = [
        # 1 and 2: the same address on two records.
        _contact(1, "dupe@acme.test", "Ada", "Byron", "(555) 010-0001",
                 companies=[901], **common),
        _contact(2, "DUPE@acme.test ", "Ada", "Byron", "(555) 010-0001",
                 companies=[901], **common),
        _contact(3, "", "Grace", "Hopper", "(555) 010-0003", companies=[902], **common),
        _contact(4, "alan@", "Alan", "Turing", "(555) 010-0004", companies=[903], **common),
        _contact(5, "test@test.com", "Test", "User", "(555) 010-0005",
                 companies=[904], **common),
        _contact(6, "info@acme.test", "Front", "Desk", "(555) 010-0006",
                 companies=[905], **common),
        _contact(7, "katherine@acme.test", "Katherine", "Johnson", "(555) 010-0007",
                 owner="", companies=[906], **common),
        _contact(8, "margaret@acme.test", "Margaret", "Hamilton", "(555) 010-0008",
                 lifecycle="", companies=[901], **common),
        # 9 and 10: one person, two addresses, same name and phone.
        _contact(9, "barbara@acme.test", "Barbara", "Liskov", "(555) 010-0009",
                 companies=[901], **common),
        _contact(10, "b.liskov@other.test", "Barbara", "Liskov", "555-010-0009",
                 companies=[901], **common),
        _contact(11, "edsger@acme.test", "Edsger", "Dijkstra", "123",
                 companies=[901], **common),
        # 12 gives the dashed format enough share to be a real minority.
        _contact(12, "donald@acme.test", "Donald", "Knuth", "555-010-0012",
                 companies=[901], **common),
        _contact(13, "linus@acme.test", "Linus", "Torvalds", "(555) 010-0013", **common),
        # 14 bought something and the CRM still says lead.
        _contact(14, "won@acme.test", "Won", "Customer", "(555) 010-0014",
                 lifecycle="lead", companies=[901], **common),
        # 15 and 16 are one person under two addresses. Each of those addresses
        # is separately duplicated (by 17 and 18), so every one of the four ids
        # appears in SOME email collision -- but 15 and 16 were never reported
        # as a pair, and must still be caught by the name+phone rule.
        _contact(15, "shared@a.test", "Ada", "Lovelace", "(555) 010-0015",
                 companies=[901], **common),
        _contact(16, "shared@b.test", "Ada", "Lovelace", "(555) 010-0015",
                 companies=[901], **common),
        _contact(17, "shared@a.test", "Charles", "Babbage", "(555) 010-0017",
                 companies=[901], **common),
        _contact(18, "shared@b.test", "Joan", "Clarke", "(555) 010-0018",
                 companies=[901], **common),
    ]

    state.objects["companies"] = [
        _company(901, "Acme Inc.", "acme.test"),
        _company(902, "Acme Corporation", "www.acme.test"),   # same domain
        _company(903, "Beta, Inc.", "beta.test"),
        _company(904, "Beta LLC", "beta-two.test"),           # same normalized name
        _company(905, "Gamma Partners", ""),                  # no domain
        _company(906, "Delta Holdings", "delta.test", owner=""),
        _company(907, "Orphan Industries", "orphan.test"),    # no contacts
    ]

    state.objects["deals"] = [
        _deal(501, contacts=[1], companies=[901]),
        _deal(502, close=PAST, contacts=[1], companies=[901]),
        _deal(503, owner="", contacts=[1], companies=[901]),
        _deal(504, amount="", contacts=[1], companies=[901]),
        _deal(505, close="", contacts=[1], companies=[901]),
        _deal(506, updated=LONG_AGO, contacts=[1], companies=[901]),
        _deal(507, companies=[901]),                          # no contact
        _deal(508, contacts=[1]),                             # no company
        _deal(509, stage="closedwon", close=PAST, contacts=[14], companies=[901]),
    ]
    return state


def clean_portal():
    """No defects at all. Every check must be able to return PASS against this."""
    state = PortalState()
    state.properties = _properties()
    state.pipelines = PIPELINE
    state.owners = [{"id": "77", "email": "rep@acme.test"}]
    common = {"legacy_import_batch": "2024-Q1", "preferred_contact_method": "email"}

    state.objects["contacts"] = [
        _contact(1, "ada@acme.test", "Ada", "Byron", "(555) 010-0001",
                 companies=[901], **common),
        _contact(2, "grace@acme.test", "Grace", "Hopper", "(555) 010-0002",
                 companies=[901], **common),
        _contact(3, "alan@acme.test", "Alan", "Turing", "(555) 010-0003",
                 lifecycle="customer", companies=[902], **common),
    ]
    state.objects["companies"] = [
        _company(901, "Acme Inc.", "acme.test"),
        _company(902, "Beta LLC", "beta.test"),
    ]
    state.objects["deals"] = [
        _deal(501, contacts=[1], companies=[901]),
        # Contact 3 is already marked customer, so a won deal is consistent.
        _deal(502, stage="closedwon", close=PAST, contacts=[3], companies=[902]),
    ]
    return state


def contacts_only_portal():
    """A free-tier shaped portal: contacts, no companies, no deals."""
    state = PortalState()
    state.properties = {"contacts": _properties()["contacts"]}
    state.objects["contacts"] = [
        _contact(1, "ada@acme.test", "Ada", "Byron", "(555) 010-0001",
                 legacy_import_batch="x", preferred_contact_method="email"),
    ]
    for absent in ("companies", "deals"):
        state.status_for_path["/crm/v3/properties/%s" % absent] = (
            404, {"status": "error", "message": "no such object type"})
    return state


def custom_lifecycle_portal():
    """A portal that replaced HubSpot's default lifecycle stages.

    The lifecycle-contradiction check has to skip itself here rather than
    guessing which custom stage means "already bought". Guessing would invent a
    conclusion about somebody's revenue.
    """
    state = dirty_portal()
    for prop_def in state.properties["contacts"]:
        if prop_def["name"] == "lifecyclestage":
            prop_def["options"] = [
                {"value": "prospect", "label": "Prospect"},
                {"value": "engaged", "label": "Engaged"},
                {"value": "client", "label": "Client"},
            ]
    return state


def no_pipeline_portal():
    """Deals exist but the portal returned no pipeline stages."""
    state = dirty_portal()
    state.pipelines = []
    return state
