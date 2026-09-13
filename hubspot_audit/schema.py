"""Ask the portal what it has before assuming anything about it.

This is the part that decides whether the tool survives contact with a real
customer. Every HubSpot portal is different: custom properties, renamed
lifecycle stages, deal pipelines that only exist in that account, and free
tiers with no deals object at all. A check that hardcodes 'lifecyclestage'
works on the author's test portal and dies on the first client.

So: discover first, then every check declares what it needs and is skipped with
a stated reason when the portal does not have it.
"""

from .errors import (
    AuthError, HubSpotError, NoAuditableObjects, NotFoundError, ScopeError,
)

# Object types the audit knows how to look at, with the read scope each needs.
OBJECT_SCOPES = {
    "contacts": "crm.objects.contacts.read",
    "companies": "crm.objects.companies.read",
    "deals": "crm.objects.deals.read",
}


class ObjectSchema:
    """What one object type looks like in this specific portal."""

    def __init__(self, object_type, properties, available=True, reason=None):
        self.object_type = object_type
        self.properties = {p["name"]: p for p in properties}
        self.available = available
        self.reason = reason

    def has(self, *names):
        """True only if every named property exists in this portal."""
        return all(name in self.properties for name in names)

    def missing(self, *names):
        return [name for name in names if name not in self.properties]

    def enum_options(self, name):
        """Allowed values for an enumeration property, as defined here.

        Lifecycle stages are the reason this exists. The defaults are
        subscriber/lead/.../customer, but portals rename and reorder them, and
        a check that hardcodes 'customer' will be wrong in a portal that calls
        it 'Client'.
        """
        prop = self.properties.get(name) or {}
        return [o.get("value") for o in (prop.get("options") or []) if "value" in o]

    def to_dict(self):
        return {
            "object_type": self.object_type,
            "available": self.available,
            "reason": self.reason,
            "property_count": len(self.properties),
        }


class Pipeline:
    def __init__(self, raw):
        self.id = raw.get("id")
        self.label = raw.get("label")
        self.archived = bool(raw.get("archived", False))
        self.stages = []
        for stage in raw.get("stages") or []:
            metadata = stage.get("metadata") or {}
            # HubSpot serialises these as the strings "true"/"false".
            is_closed = str(metadata.get("isClosed", "false")).lower() == "true"
            try:
                probability = float(metadata.get("probability", 0) or 0)
            except (TypeError, ValueError):
                probability = 0.0
            self.stages.append({
                "id": stage.get("id"),
                "label": stage.get("label"),
                "is_closed": is_closed,
                "probability": probability,
                "display_order": stage.get("displayOrder"),
            })

    @property
    def open_stage_ids(self):
        return {s["id"] for s in self.stages if not s["is_closed"]}

    @property
    def closed_won_stage_ids(self):
        """The highest-probability closed stage in this pipeline.

        NOT "probability == 1.0". Nothing requires a Closed Won stage to sit at
        1.0, and HubSpot's own documentation shows real portals with Closed Won
        at 0.8. Requiring 1.0 makes the lifecycle-contradiction check silently
        skip itself on those portals.
        """
        closed = [s for s in self.stages if s["is_closed"]]
        if not closed:
            return set()
        best = max(s["probability"] for s in closed)
        # A pipeline whose closed stages are all at 0.0 has no winning stage,
        # only losing ones.
        if best <= 0:
            return set()
        return {s["id"] for s in closed if s["probability"] >= best}

    def to_dict(self):
        return {"id": self.id, "label": self.label, "stages": self.stages}


class PortalProfile:
    """The discovered shape of a portal, handed to every check."""

    def __init__(self, schemas=None, pipelines=None, owners=None):
        self.schemas = schemas or {}
        self.pipelines = pipelines or []
        self.owners = owners or []

    def schema(self, object_type):
        return self.schemas.get(object_type) or ObjectSchema(
            object_type, [], available=False,
            reason="object type was not discovered in this portal")

    def available(self, object_type):
        return self.schema(object_type).available

    @property
    def open_stage_ids(self):
        ids = set()
        for pipeline in self.pipelines:
            ids |= pipeline.open_stage_ids
        return ids

    @property
    def closed_won_stage_ids(self):
        ids = set()
        for pipeline in self.pipelines:
            ids |= pipeline.closed_won_stage_ids
        return ids

    @property
    def owner_ids(self):
        return {str(o.get("id")) for o in self.owners if o.get("id") is not None}

    def to_dict(self):
        return {
            "objects": {k: v.to_dict() for k, v in sorted(self.schemas.items())},
            "pipelines": [p.to_dict() for p in self.pipelines],
            "owner_count": len(self.owners),
        }


def discover(client, object_types=None):
    """Build a PortalProfile by asking the portal what exists.

    Never raises for a missing object type or a missing scope. Those are facts
    about the portal, recorded on the profile, and the checks that needed them
    report NOT_RUN. The audit should still produce a useful report for a
    free-tier portal with contacts and nothing else.

    A bad token is NOT such a fact. AuthError is allowed to propagate, because
    swallowing it produces the worst possible output: a clean-looking report in
    which every check was skipped, handed to someone who will read the absence
    of findings as good news. Discovering nothing and discovering nothing wrong
    must never look the same.
    """
    object_types = object_types or list(OBJECT_SCOPES)
    schemas = {}

    for object_type in object_types:
        scope = OBJECT_SCOPES.get(object_type)
        try:
            body = client.get("/crm/v3/properties/%s" % object_type,
                              required_scope=scope)
            schemas[object_type] = ObjectSchema(object_type, body.get("results") or [])
        except ScopeError:
            schemas[object_type] = ObjectSchema(
                object_type, [], available=False,
                reason="the private app is missing the %s scope" % scope)
        except NotFoundError:
            schemas[object_type] = ObjectSchema(
                object_type, [], available=False,
                reason="this portal does not have the %s object" % object_type)
        except AuthError:
            raise  # a rejected token invalidates the whole run, not one object
        except HubSpotError as exc:
            schemas[object_type] = ObjectSchema(
                object_type, [], available=False,
                reason="could not read %s properties: %s" % (object_type, exc))

    pipelines = []
    if schemas.get("deals") and schemas["deals"].available:
        try:
            body = client.get("/crm/v3/pipelines/deals",
                              required_scope=OBJECT_SCOPES["deals"])
            # Archived pipelines still come back on this endpoint and there is
            # no query parameter to exclude them. Their stage ids would
            # otherwise pollute every deal-stage judgement.
            pipelines = [p for p in (Pipeline(raw) for raw in body.get("results") or [])
                         if not p.archived]
        except HubSpotError:
            # No pipelines means deal-stage checks skip themselves. Not fatal.
            pipelines = []

    owners = []
    try:
        # Paginated, default page size 100. A portal with more seats than that
        # would silently return a truncated list.
        owners = list(client.paginate("/crm/v3/owners",
                                      required_scope="crm.objects.owners.read"))
    except AuthError:
        raise
    except HubSpotError:
        owners = []

    profile = PortalProfile(schemas=schemas, pipelines=pipelines, owners=owners)

    # If nothing at all could be read, there is no audit to run. Saying so is
    # the only honest outcome; returning an all-skipped report is not.
    if not any(schema.available for schema in schemas.values()):
        reasons = "; ".join(
            "%s: %s" % (name, schema.reason)
            for name, schema in sorted(schemas.items()) if schema.reason)
        raise NoAuditableObjects(
            "None of the CRM objects could be read, so no audit is possible. "
            + reasons)

    return profile
