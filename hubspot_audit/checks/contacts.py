"""Contact-level hygiene checks.

Ordering note on duplicates: email is checked first and a contact already
flagged as an email duplicate is not re-reported by the weaker name+phone rule.
Otherwise the same pair is counted twice and the headline number lies.
"""

from ..phrasing import have, is_are, n_of
from ..models import Finding, Severity
from ..normalize import (
    clean, email_is_placeholder, email_is_role_account, email_is_wellformed,
    is_placeholder, normalize_email, normalize_name, normalize_phone,
    phone_format_family, phone_is_plausible,
)
from .base import Check, run_check


class DuplicateEmail(Check):
    """Two contacts, one email address. The most expensive duplicate there is.

    HubSpot enforces email uniqueness on creation through the UI, so every
    instance of this got in through an import or the API -- which is exactly
    how a contact ends up receiving the same campaign twice.
    """

    check_id = "contacts.duplicate_email"
    title = "Contacts sharing an email address"
    object_type = "contacts"
    severity = Severity.HIGH
    needs_properties = ("email",)

    def __init__(self):
        self.by_email = {}

    def observe(self, record):
        email = normalize_email(self.prop(record, "email"))
        if not email or email_is_placeholder(email):
            return
        self.by_email.setdefault(email, []).append(record["id"])

    def duplicate_groups(self):
        """Map of contact id -> the email that grouped it.

        The name+phone rule uses this to avoid reporting the same pair twice.
        It has to be a map and not a flat set of ids: with a set, a group whose
        members each appear in DIFFERENT email collisions looks "already
        reported" even though that particular pair never was, and a genuine
        duplicate gets silently dropped.
        """
        groups = {}
        for email, contact_ids in self.by_email.items():
            if len(contact_ids) > 1:
                for cid in contact_ids:
                    groups[cid] = email
        return groups

    def finish(self):
        affected, groups = [], 0
        for email, contact_ids in sorted(self.by_email.items()):
            if len(contact_ids) > 1:
                groups += 1
                affected.extend(contact_ids)
        if not affected:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s %s" % (n_of(len(affected), "contact"),
                          "share" if len(affected) != 1 else "shares",
                          n_of(groups, "email address", "email addresses")),
            "Each of these email addresses appears on more than one contact "
            "record. Campaigns send once per record, so these people receive "
            "duplicate email, and any per-contact reporting double counts them.",
            self.object_type, affected,
            rule="exact match on lowercased, trimmed email",
            evidence={"duplicate_groups": groups})]


class DuplicateNamePhone(Check):
    """Same person, different email. Caught on name plus phone.

    Weaker evidence than an email match, so it is reported as a review queue
    rather than a confirmed duplicate. Two people can share a name; two people
    sharing a name AND a phone number is usually one person entered twice, but
    it is also how a household or a shared office line looks.
    """

    check_id = "contacts.duplicate_name_phone"
    title = "Probable duplicate contacts (same name and phone)"
    object_type = "contacts"
    severity = Severity.MEDIUM
    needs_properties = ("firstname", "lastname", "phone")

    def __init__(self, email_check=None):
        self.by_key = {}
        self._email_check = email_check

    def observe(self, record):
        name = normalize_name(self.prop(record, "firstname"),
                              self.prop(record, "lastname"))
        phone = normalize_phone(self.prop(record, "phone"))
        # Both halves required. Name alone is far too weak, phone alone catches
        # every shared office line in the portal.
        if not name or not phone or not phone_is_plausible(phone):
            return
        self.by_key.setdefault((name, phone), []).append(record["id"])

    def finish(self):
        already = self._email_check.duplicate_groups() if self._email_check else {}
        affected, groups = [], 0
        for _key, contact_ids in sorted(self.by_key.items()):
            if len(contact_ids) < 2:
                continue
            # Skip only when this exact group is already one email group. Two
            # contacts that each belong to a different email collision have
            # never been reported as a pair, and dropping them would hide a
            # real duplicate.
            keys = {already.get(cid) for cid in contact_ids}
            if len(keys) == 1 and None not in keys:
                continue
            groups += 1
            affected.extend(contact_ids)
        if not affected:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s look like %s entered twice"
            % (n_of(len(affected), "contact"), n_of(groups, "person", "people")),
            "These records share a normalized name and phone number but not an "
            "email address. Usually one person entered twice under two "
            "addresses. Review before merging: a shared household or office "
            "line produces the same pattern.",
            self.object_type, affected,
            rule="exact match on accent-folded name plus digits-only phone, "
                 "excluding pairs already reported as email duplicates",
            evidence={"review_groups": groups})]


class MissingEmail(Check):
    check_id = "contacts.missing_email"
    title = "Contacts with no email address"
    object_type = "contacts"
    severity = Severity.MEDIUM
    needs_properties = ("email",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        raw = self.prop(record, "email")
        if not clean(raw) or is_placeholder(raw):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s no usable email address"
            % (n_of(len(self.ids), "contact"), have(len(self.ids))),
            "These records cannot be emailed and cannot be deduplicated "
            "against future imports, which is how the same person gets added "
            "again next quarter.",
            self.object_type, self.ids,
            rule="email is empty, whitespace, or a placeholder value such as "
                 "'n/a', 'none' or 'unknown'")]


class MalformedEmail(Check):
    check_id = "contacts.malformed_email"
    title = "Contacts with a malformed email address"
    object_type = "contacts"
    severity = Severity.HIGH
    needs_properties = ("email",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        raw = self.prop(record, "email")
        if not clean(raw) or is_placeholder(raw):
            return  # absence is MissingEmail's job, not ours
        if not email_is_wellformed(raw):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s an email address that cannot be delivered"
            % (n_of(len(self.ids), "contact"), have(len(self.ids))),
            "Populated but structurally invalid: a missing @, a missing "
            "domain, a trailing comma from a paste. Every send to these "
            "records bounces, and bounce rate is what gets a sending domain "
            "throttled.",
            self.object_type, self.ids,
            rule="email is non-empty but fails a local-part@domain.tld shape test")]


class PlaceholderEmail(Check):
    check_id = "contacts.placeholder_email"
    title = "Contacts with a placeholder email address"
    object_type = "contacts"
    severity = Severity.MEDIUM
    needs_properties = ("email",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        if email_is_placeholder(self.prop(record, "email")):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s a placeholder email address"
            % (n_of(len(self.ids), "contact"),
               "carries" if len(self.ids) == 1 else "carry"),
            "Addresses like test@test.com or anything at example.com. They "
            "pass validation, they count toward marketing contact tiers, and "
            "they reach nobody.",
            self.object_type, self.ids,
            rule="email matches a known placeholder address or sits on a "
                 "reserved example domain")]


class RoleAccountEmail(Check):
    check_id = "contacts.role_account_email"
    title = "Shared inboxes stored as people"
    object_type = "contacts"
    severity = Severity.LOW
    needs_properties = ("email",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        if email_is_role_account(self.prop(record, "email")):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s a shared inbox, not a person"
            % (n_of(len(self.ids), "contact"), is_are(len(self.ids))),
            "info@, sales@, support@ and similar. Personalized sends to these "
            "read badly, and attributing activity to them distorts any "
            "per-person engagement metric.",
            self.object_type, self.ids,
            rule="email local part is a known role account name")]


class NoOwner(Check):
    check_id = "contacts.no_owner"
    title = "Contacts with no owner"
    object_type = "contacts"
    severity = Severity.MEDIUM
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
            % (n_of(len(self.ids), "contact"), have(len(self.ids))),
            "Unowned records fall out of every owner-filtered view and report, "
            "so in practice nobody is following up on them.",
            self.object_type, self.ids,
            rule="hubspot_owner_id is empty")]


class MissingLifecycleStage(Check):
    check_id = "contacts.missing_lifecycle_stage"
    title = "Contacts with no lifecycle stage"
    object_type = "contacts"
    severity = Severity.MEDIUM
    needs_properties = ("lifecyclestage",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        if not clean(self.prop(record, "lifecyclestage")):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s no lifecycle stage"
            % (n_of(len(self.ids), "contact"), have(len(self.ids))),
            "Lifecycle stage is what funnel reporting counts. Blank records are "
            "invisible to it, so the funnel is understated by this many people.",
            self.object_type, self.ids,
            rule="lifecyclestage is empty")]


class PhoneFormatInconsistency(Check):
    """Not a per-record fault. A portal-level observation about consistency."""

    check_id = "contacts.phone_format_inconsistency"
    title = "Phone numbers stored in inconsistent formats"
    object_type = "contacts"
    severity = Severity.LOW
    needs_properties = ("phone",)

    #: Below this share of contacts, a format is noise rather than a pattern.
    MINORITY_THRESHOLD = 0.10

    def __init__(self):
        self.families = {}
        self.ids_by_family = {}

    def observe(self, record):
        raw = self.prop(record, "phone")
        if not clean(raw):
            return
        family = phone_format_family(raw)
        self.families[family] = self.families.get(family, 0) + 1
        self.ids_by_family.setdefault(family, []).append(record["id"])

    def finish(self):
        total = sum(self.families.values())
        if total == 0 or len(self.families) < 2:
            return []
        ranked = sorted(self.families.items(), key=lambda kv: -kv[1])
        dominant, dominant_count = ranked[0]
        # Only report when a real minority exists. A portal that is 99.8% one
        # format with two odd rows does not have a consistency problem.
        minority = [(fam, n) for fam, n in ranked[1:]
                    if n / total >= self.MINORITY_THRESHOLD]
        if not minority:
            return []
        affected = []
        for family, _n in minority:
            affected.extend(self.ids_by_family[family])
        return [Finding(
            self.check_id, self.severity,
            "Phone numbers are stored in %s"
            % n_of(len(self.families), "different format"),
            "%d of %d populated numbers sit outside the dominant '%s' format, "
            "%d of them in a minority format common enough to be a pattern "
            "rather than a typo. Mixed formats break dialer integrations and "
            "stop phone from working as a deduplication key."
            % (total - dominant_count, total, dominant, len(affected)),
            self.object_type, affected,
            rule="phone values grouped by punctuation style; any style holding "
                 "at least %d%% of populated numbers outside the dominant one "
                 "is reported" % int(self.MINORITY_THRESHOLD * 100),
            evidence={"formats": dict(ranked), "dominant": dominant,
                      "dominant_count": dominant_count})]


class ImplausiblePhone(Check):
    check_id = "contacts.implausible_phone"
    title = "Phone numbers that cannot be real"
    object_type = "contacts"
    severity = Severity.LOW
    needs_properties = ("phone",)

    def __init__(self):
        self.ids = []

    def observe(self, record):
        raw = self.prop(record, "phone")
        if not clean(raw):
            return
        if not phone_is_plausible(raw):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        return [Finding(
            self.check_id, self.severity,
            "%s %s an unusable phone number"
            % (n_of(len(self.ids), "contact"), have(len(self.ids))),
            "Fewer than 7 or more than 15 digits once punctuation is removed. "
            "Usually an extension typed into the phone field, or a partial "
            "paste.",
            self.object_type, self.ids,
            rule="digit count outside 7-15 after stripping non-digits")]


class NoCompanyAssociation(Check):
    """Only meaningful for B2B portals, so it reports its own context."""

    check_id = "contacts.no_company_association"
    title = "Contacts not linked to a company"
    object_type = "contacts"
    severity = Severity.MEDIUM
    needs_properties = ()
    needs_associations = ("companies",)

    def __init__(self):
        self.ids = []
        self.total = 0

    def requires(self, profile):
        base = super().requires(profile)
        if base:
            return base
        if not profile.available("companies"):
            return "this portal has no companies object, so contacts cannot be linked to one"
        return None

    def observe(self, record):
        self.total += 1
        if not self.associated_ids(record, "companies"):
            self.ids.append(record["id"])

    def finish(self):
        if not self.ids:
            return []
        share = (len(self.ids) / self.total) if self.total else 0
        return [Finding(
            self.check_id, self.severity,
            "%s (%d%%) %s not linked to a company"
            % (n_of(len(self.ids), "contact"), round(share * 100), is_are(len(self.ids))),
            "Unlinked contacts drop out of account-level reporting and every "
            "company-based view. If this portal is B2C the number is expected "
            "and can be ignored.",
            self.object_type, self.ids,
            rule="contact has no association to any company record",
            evidence={"contacts_examined": self.total})]


def build(profile):
    """Instantiate the contact checks, wired so duplicates do not double count."""
    email_dupes = DuplicateEmail()
    return [
        email_dupes,
        DuplicateNamePhone(email_check=email_dupes),
        MissingEmail(),
        MalformedEmail(),
        PlaceholderEmail(),
        RoleAccountEmail(),
        NoOwner(),
        MissingLifecycleStage(),
        PhoneFormatInconsistency(),
        ImplausiblePhone(),
        NoCompanyAssociation(),
    ]
