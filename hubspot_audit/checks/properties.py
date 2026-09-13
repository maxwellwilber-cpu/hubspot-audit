"""Schema-level checks: fields that exist but carry no data.

Dead custom properties are the archaeology of a CRM. Someone built a field for
a campaign in 2023, nobody filled it in after the first month, and it is still
on every form and every record view, adding noise to the work of whoever has to
use the thing daily.
"""

from ..phrasing import have, is_are, n_of
from ..models import Finding, Severity
from ..normalize import clean
from .base import Check

#: Requesting properties costs URL length, and HubSpot will reject a request
#: that gets too long. Cap the number of custom properties sampled and say so
#: in the finding rather than silently truncating.
MAX_CUSTOM_PROPERTIES = 40


class UnusedCustomProperties(Check):
    check_id = "schema.unused_custom_properties"
    title = "Custom properties that are never populated"
    object_type = "contacts"
    severity = Severity.LOW

    def __init__(self, object_type="contacts"):
        self.object_type = object_type
        self.check_id = "schema.unused_custom_properties.%s" % object_type
        self.title = "Custom %s properties that are never populated" % object_type
        self.counts = {}
        self.records_seen = 0
        self._examined = []
        self._total_custom = 0

    def requires(self, profile):
        schema = profile.schema(self.object_type)
        if not schema.available:
            return schema.reason or "this portal has no %s object" % self.object_type
        custom = [
            name for name, prop in sorted(schema.properties.items())
            # hubspotDefined False means someone in this portal created it.
            # Calculated properties are derived and legitimately empty until
            # their inputs are, so they are not evidence of neglect.
            #
            # The `calculated` flag alone is not enough: HubSpot documents it as
            # having no effect for CUSTOM properties, which is exactly the set
            # being examined here. A user-built rollup therefore has to be
            # recognised by its calculationFormula instead, or the tool
            # recommends deleting a working calculation -- and property
            # deletion is irreversible.
            if not prop.get("hubspotDefined", False)
            and not prop.get("calculated", False)
            and not clean(prop.get("calculationFormula"))
            and not prop.get("archived", False)
        ]
        self._total_custom = len(custom)
        if not custom:
            return "this portal has no custom %s properties" % self.object_type
        self._examined = custom[:MAX_CUSTOM_PROPERTIES]
        self.counts = {name: 0 for name in self._examined}
        return None

    @property
    def needs_properties(self):
        # Resolved after requires() has run, which is when the runner reads it.
        return tuple(self._examined)

    def observe(self, record):
        self.records_seen += 1
        props = record.get("properties") or {}
        for name in self._examined:
            if clean(props.get(name)):
                self.counts[name] += 1

    def finish(self):
        if not self.records_seen:
            return []
        unused = sorted(name for name, count in self.counts.items() if count == 0)
        if not unused:
            return []
        detail = (
            "These custom fields are defined on every %s record and populated "
            "on none of them. They clutter forms, record views and imports, "
            "and each one is a place for someone to put data that nothing "
            "reads." % self.object_type)
        if self._total_custom > len(self._examined):
            detail += (" Sampled the first %d of %d custom properties."
                       % (len(self._examined), self._total_custom))
        return [Finding(
            self.check_id, self.severity,
            "%s on %s %s no data"
            % (n_of(len(unused), "custom property", "custom properties"),
               self.object_type,
               "holds" if len(unused) == 1 else "hold"),
            detail,
            self.object_type,
            # The affected "records" here are property names, not record ids.
            # Named explicitly in the rule so the CSV is not misread.
            record_ids=unused,
            rule="custom (not HubSpot-defined, not calculated, not archived) "
                 "property is empty on every %s record scanned" % self.object_type,
            evidence={"records_scanned": self.records_seen,
                      "custom_properties_examined": len(self._examined),
                      "custom_properties_total": self._total_custom,
                      "unit": "property names, not record ids"})]


def build(profile):
    checks = []
    for object_type in ("contacts", "companies", "deals"):
        if profile.available(object_type):
            checks.append(UnusedCustomProperties(object_type))
    return checks
