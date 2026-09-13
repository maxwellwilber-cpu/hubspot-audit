"""Company-level hygiene checks."""

from ..phrasing import have, is_are, n_of
from ..models import Finding, Severity
from ..normalize import clean, is_placeholder, normalize_company, normalize_domain
from .base import Check


class DuplicateDomain(Check):
    """Two company records, one web domain. Almost always one real company."""

    check_id = "companies.duplicate_domain"
    title = "Companies sharing a domain"
    object_type = "companies"
    severity = Severity.HIGH
    needs_properties = ("domain",)

    def __init__(self):
        self.by_domain = {}

    def observe(self, record):
        domain = normalize_domain(self.prop(record, "domain"))
        if not domain:
            return
        self.by_domain.setdefault(domain, []).append(record["id"])

    def duplicate_groups(self):
        """Map of company id -> the domain that grouped it. See the contacts
        version for why this cannot be a flat set of ids."""
        groups = {}
        for domain, company_ids in self.by_domain.items():
            if len(company_ids) > 1:
                for cid in company_ids:
                    groups[cid] = domain
        return groups

    def finish(self):
        affected, groups = [], 0
        for _domain, company_ids in sorted(self.by_domain.items()):
            if len(company_ids) > 1:
                groups += 1
                affected.extend(company_ids)
        if not affected:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s cover %s"
            % (n_of(len(affected), "company record"), n_of(groups, "domain")),
            "Contacts, deals and activity are split across these records, so "
            "no single one shows the real relationship with the account. This "
            "is the duplicate that most often loses a renewal.",
            self.object_type, affected,
            rule="exact match on domain after stripping scheme, www and path",
            evidence={"duplicate_groups": groups})]


class DuplicateCompanyName(Check):
    """Same company, different spelling. Weaker evidence, so: review queue.

    'Acme, Inc.' and 'Acme LLC' normalize to the same string. Sometimes that is
    one company entered twice and sometimes it is two genuinely distinct legal
    entities. The finding says which and does not pretend to be sure.
    """

    check_id = "companies.duplicate_name"
    title = "Companies with matching normalized names"
    object_type = "companies"
    severity = Severity.MEDIUM
    needs_properties = ("name",)

    def __init__(self, domain_check=None):
        self.by_name = {}
        self._domain_check = domain_check

    def observe(self, record):
        name = normalize_company(self.prop(record, "name"))
        if not name:
            return
        self.by_name.setdefault(name, []).append(record["id"])

    def finish(self):
        already = self._domain_check.duplicate_groups() if self._domain_check else {}
        affected, groups = [], 0
        for _name, company_ids in sorted(self.by_name.items()):
            if len(company_ids) < 2:
                continue
            # Skip only when this exact group is already one domain group.
            keys = {already.get(cid) for cid in company_ids}
            if len(keys) == 1 and None not in keys:
                continue
            groups += 1
            affected.extend(company_ids)
        if not affected:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s fall into %s"
            % (n_of(len(affected), "company", "companies"),
               n_of(groups, "matching-name group")),
            "Names match once punctuation and legal suffixes are removed, but "
            "the domains differ or are missing. Review before merging: "
            "subsidiaries and franchises look identical under this rule.",
            self.object_type, affected,
            rule="exact match on lowercased name with punctuation and legal "
                 "suffixes (inc, llc, ltd, gmbh...) removed, excluding groups "
                 "already reported as domain duplicates",
            evidence={"review_groups": groups})]


class MissingDomain(Check):
    check_id = "companies.missing_domain"
    title = "Companies with no domain"
    object_type = "companies"
    severity = Severity.MEDIUM
    needs_properties = ("domain",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        raw = self.prop(record, "domain")
        if not clean(raw) or is_placeholder(raw):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s no domain"
            % (n_of(len(self.ids), "company", "companies"), have(len(self.ids))),
            "Domain is how HubSpot links new contacts to an existing company "
            "automatically. Without it, every future contact from that "
            "business lands unlinked or creates a second company record.",
            self.object_type, self.ids,
            rule="domain is empty or a placeholder value")]


class CompanyNoOwner(Check):
    check_id = "companies.no_owner"
    title = "Companies with no owner"
    object_type = "companies"
    severity = Severity.LOW
    needs_properties = ("hubspot_owner_id",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        if not clean(self.prop(record, "hubspot_owner_id")):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s nobody assigned"
            % (n_of(len(self.ids), "company", "companies"), have(len(self.ids))),
            "Unowned accounts sit outside every territory and owner-filtered "
            "report.",
            self.object_type, self.ids,
            rule="hubspot_owner_id is empty")]


def build(profile):
    domain_dupes = DuplicateDomain()
    return [
        domain_dupes,
        DuplicateCompanyName(domain_check=domain_dupes),
        MissingDomain(),
        CompanyNoOwner(),
    ]
