"""Checks that need two object types at once.

These are the ones worth having. Anyone can count blank fields; noticing that a
contact is still marked a lead while carrying a closed-won deal requires
knowing how the two objects relate, and it is the kind of contradiction that
quietly breaks revenue reporting for a year.

Cross-object checks subscribe to several record streams. The runner pages each
object type once and hands every record to whichever checks asked for it, so
adding these costs no extra API calls.
"""

from ..dealstage import OPTIONAL_PROPERTIES, StageResolver
from ..phrasing import have, is_are, n_of
from ..models import Finding, Severity
from ..normalize import clean
from .base import Check

#: HubSpot's internal value for the customer lifecycle stage. Portals can
#: relabel it in the UI without changing this, but a portal that deleted it and
#: built custom stages will not have it -- which is why the check verifies it
#: exists before running rather than assuming.
CUSTOMER_STAGE = "customer"


class CrossCheck(Check):
    """A check fed by more than one object stream."""

    streams = ()

    def requires(self, profile):
        """Every stream must exist and carry the properties declared for it.

        Subclasses that override this must call it. Without this, a cross-object
        check reading a property the portal does not have sees a blank string on
        every record, finds nothing, and reports PASS -- the exact silent pass
        that per-check property declarations exist to prevent.
        """
        for stream in self.streams:
            schema = profile.schema(stream)
            if not schema.available:
                return schema.reason or "this portal has no %s object" % stream
            missing = schema.missing(*self.stream_properties.get(stream, ()))
            if missing:
                return "this portal has no %s property on %s" % (
                    ", ".join(missing), stream)
        return None

    def observe(self, record):  # pragma: no cover - cross checks use observe_stream
        raise NotImplementedError("cross-object checks implement observe_stream")

    def observe_stream(self, object_type, record):
        raise NotImplementedError

    #: Properties needed per stream, rather than one flat tuple.
    stream_properties = {}

    #: Per stream, properties requested when available but not required.
    stream_optional_properties = {}

    #: Associations needed per stream.
    stream_associations = {}


class DealWithoutContact(CrossCheck):
    check_id = "crossobject.deal_without_contact"
    title = "Deals not linked to any contact"
    object_type = "deals"
    severity = Severity.HIGH
    streams = ("deals",)
    stream_associations = {"deals": ("contacts",)}

    def __init__(self):
        self.ids = []

    def requires(self, profile):
        base = super().requires(profile)
        if base:
            return base
        if not profile.available("contacts"):
            return "this portal has no contacts object to link deals to"
        return None

    def observe_stream(self, object_type, record):
        if object_type != "deals":
            return
        if not self.associated_ids(record, "contacts"):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s not linked to any contact"
            % (n_of(len(self.ids), "deal"), is_are(len(self.ids))),
            "There is no person attached to these deals, so nobody knows who "
            "to call, and none of them appear in contact-level attribution. "
            "This is the usual fingerprint of deals created by an import or an "
            "integration rather than by a rep.",
            self.object_type, self.ids,
            rule="deal has no association to any contact record")]


class DealWithoutCompany(CrossCheck):
    check_id = "crossobject.deal_without_company"
    title = "Deals not linked to any company"
    object_type = "deals"
    severity = Severity.MEDIUM
    streams = ("deals",)
    stream_associations = {"deals": ("companies",)}

    def __init__(self):
        self.ids = []

    def requires(self, profile):
        base = super().requires(profile)
        if base:
            return base
        if not profile.available("companies"):
            return "this portal has no companies object to link deals to"
        return None

    def observe_stream(self, object_type, record):
        if object_type != "deals":
            return
        if not self.associated_ids(record, "companies"):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s not linked to a company"
            % (n_of(len(self.ids), "deal"), is_are(len(self.ids))),
            "Account-level revenue reporting misses these entirely, so any "
            "'how much has this customer spent' question returns the wrong "
            "number.",
            self.object_type, self.ids,
            rule="deal has no association to any company record")]


class LifecycleContradictsDeal(CrossCheck):
    """A contact with a closed-won deal who is not marked a customer.

    The check that shows you have actually run a business. It is a
    contradiction between two systems of record inside the same CRM, and it
    means the marketing team is still nurturing people who already bought.
    """

    check_id = "crossobject.lifecycle_contradicts_deal"
    title = "Won deals whose contacts are not marked customers"
    object_type = "contacts"
    severity = Severity.HIGH
    streams = ("contacts", "deals")
    stream_properties = {"contacts": ("lifecyclestage",), "deals": ("dealstage",)}
    stream_optional_properties = {"deals": OPTIONAL_PROPERTIES}
    stream_associations = {"deals": ("contacts",)}

    def __init__(self):
        # Only non-customer contacts are retained, which keeps this small on a
        # portal where most contacts have already converted.
        self.non_customer_contacts = set()
        self.contacts_seen = 0
        self.won_contact_ids = set()
        self._resolver = None

    def requires(self, profile):
        base = super().requires(profile)
        if base:
            return base
        schema = profile.schema("contacts")
        if not schema.has("lifecyclestage"):
            return "this portal has no lifecyclestage property on contacts"
        options = schema.enum_options("lifecyclestage")
        # A portal that replaced the default stages with custom ones has no
        # 'customer' value, and guessing which custom stage means the same
        # thing would be inventing a conclusion.
        if options and CUSTOMER_STAGE not in options:
            return ("this portal uses custom lifecycle stages with no '%s' "
                    "value, so 'already a customer' cannot be determined"
                    % CUSTOMER_STAGE)
        self._resolver = StageResolver(profile)
        return self._resolver.won_reason()

    def observe_stream(self, object_type, record):
        if object_type == "contacts":
            self.contacts_seen += 1
            stage = clean(self.prop(record, "lifecyclestage")).lower()
            if stage != CUSTOMER_STAGE:
                self.non_customer_contacts.add(record["id"])
        elif object_type == "deals":
            if self._resolver.is_won(record):
                self.won_contact_ids.update(self.associated_ids(record, "contacts"))

    def finish(self):
        affected = sorted(self.won_contact_ids & self.non_customer_contacts,
                          key=lambda i: (len(i), i))
        if not affected:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s a won deal but %s not marked as %s"
            % (n_of(len(affected), "contact"), have(len(affected)),
               is_are(len(affected)),
               "a customer" if len(affected) == 1 else "customers"),
            "These people bought and the CRM still says they did not. They "
            "stay in lead nurture sequences, they are counted as open funnel, "
            "and any conversion rate calculated from lifecycle stage is wrong "
            "by at least this many.",
            self.object_type, affected,
            rule="contact is associated with a deal the portal reports as "
                 "closed-won (via %s), while the contact's lifecyclestage is "
                 "not '%s'" % (self._resolver.source, CUSTOMER_STAGE),
            evidence={"contacts_examined": self.contacts_seen})]


class CompanyWithoutContacts(CrossCheck):
    check_id = "crossobject.company_without_contacts"
    title = "Companies with no contacts attached"
    object_type = "companies"
    severity = Severity.LOW
    streams = ("contacts", "companies")
    stream_associations = {"contacts": ("companies",)}

    def __init__(self):
        self.company_ids = []
        self.linked_company_ids = set()

    def requires(self, profile):
        return super().requires(profile)

    def observe_stream(self, object_type, record):
        if object_type == "companies":
            self.company_ids.append(record["id"])
        elif object_type == "contacts":
            self.linked_company_ids.update(self.associated_ids(record, "companies"))

    def finish(self):
        orphans = [cid for cid in self.company_ids if cid not in self.linked_company_ids]
        if not orphans:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s no contacts attached"
            % (n_of(len(orphans), "company", "companies"), have(len(orphans))),
            "Account records with nobody in them. Usually left behind by an "
            "import, or by contacts being deleted or merged away underneath "
            "them.",
            self.object_type, orphans,
            rule="company has no contact associated to it from the contacts side",
            evidence={"companies_examined": len(self.company_ids)})]


def build(profile):
    return [
        DealWithoutContact(),
        DealWithoutCompany(),
        LifecycleContradictsDeal(),
        CompanyWithoutContacts(),
    ]
