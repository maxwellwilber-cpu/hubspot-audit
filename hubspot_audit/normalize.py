"""Normalizing the fields duplicates hide behind.

Everything here is deliberately conservative. Over-normalizing invents matches:
if you strip every dot from a Gmail address you will merge two real people who
happen to share a name pattern, and a wrong merge in someone's CRM is a much
worse outcome than a missed one. So the rules below only collapse differences
that are genuinely cosmetic.
"""

import re
import unicodedata

_WS = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D+")

# Deliberately loose. This is not RFC 5322 and does not try to be; it exists to
# catch the things a human typed wrong, like a missing @ or a trailing comma.
_EMAIL_SHAPE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")

# Addresses that are structurally valid but are not a person.
_ROLE_LOCALPARTS = {
    "info", "sales", "support", "admin", "contact", "hello", "office",
    "billing", "accounts", "help", "team", "enquiries", "inquiries",
    "noreply", "no-reply", "donotreply", "postmaster", "webmaster",
}

# Placeholder values that mean "someone had to put something in this box".
_PLACEHOLDER_VALUES = {
    "", "n/a", "na", "none", "null", "nil", "-", "--", ".", "..",
    "test", "testing", "asdf", "asdfasdf", "qwerty", "xxx", "xx", "x",
    "unknown", "tbd", "to be determined", "no email", "noemail",
    "not provided", "notprovided", "placeholder", "sample", "example",
    "first", "last", "firstname", "lastname", "fname", "lname",
    "do not use", "donotuse", "delete", "duplicate", "dupe",
}

_PLACEHOLDER_EMAILS = {
    "test@test.com", "test@test.test", "a@a.com", "x@x.com",
    "noemail@noemail.com", "none@none.com", "no@email.com",
    "email@example.com", "user@example.com", "test@example.com",
}

_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "test.com", "localhost"}


def clean(value):
    """Trim, collapse whitespace, and treat HubSpot's empties as empty."""
    if value is None:
        return ""
    text = str(value).replace(" ", " ")
    return _WS.sub(" ", text).strip()


def is_placeholder(value):
    """True when a field is technically populated but carries no information."""
    text = clean(value).lower().strip(" .,-_")
    return text in _PLACEHOLDER_VALUES


def normalize_email(value):
    """Lowercase and trim. Nothing else.

    Specifically NOT doing: stripping dots, stripping +tags, or unifying
    googlemail with gmail. Those are true for Gmail's routing and false as a
    statement about identity, and acting on them merges people who asked to be
    kept apart.
    """
    text = clean(value).lower()
    # Paste accidents: "Name <a@b.com>" and trailing punctuation.
    if "<" in text and ">" in text:
        inner = text[text.rfind("<") + 1:text.rfind(">")].strip()
        if inner:
            text = inner
    return text.strip(" ,;:")


def email_is_wellformed(value):
    return bool(_EMAIL_SHAPE.match(normalize_email(value)))


def email_domain(value):
    email = normalize_email(value)
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def email_is_role_account(value):
    """shared inbox rather than a person: info@, sales@, support@."""
    email = normalize_email(value)
    if "@" not in email:
        return False
    local = email.split("@", 1)[0]
    # Strip a +tag before comparing so "sales+web@" still counts.
    local = local.split("+", 1)[0]
    return local in _ROLE_LOCALPARTS


def email_is_placeholder(value):
    email = normalize_email(value)
    if not email:
        return False
    if email in _PLACEHOLDER_EMAILS:
        return True
    return email_domain(email) in _PLACEHOLDER_DOMAINS


def normalize_phone(value):
    """Digits only, with US country code dropped so formats compare equal.

    +1 (555) 010-9999, 555-010-9999 and 5550109999 are the same number typed
    three ways, and treating them as three numbers is how a CRM ends up with
    three of the same person.
    """
    digits = _NON_DIGIT.sub("", clean(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def phone_is_plausible(value):
    """Length sanity only. Real validation needs the country, which we lack."""
    digits = normalize_phone(value)
    return 7 <= len(digits) <= 15


def normalize_name(first, last):
    """Accent-folded, punctuation-stripped, lowercased 'first last'.

    Returns "" if either supplied piece was dropped as a placeholder. Silently
    dropping one half would leave a single-token key, and "na", "nil" and "x"
    are all in the placeholder list and all real surnames: "Grace Na", "Grace
    X" and a surname-less "Grace" would otherwise collapse onto 'grace' and be
    reported as one person entered three times.
    """
    parts = []
    for piece in (first, last):
        text = clean(piece).lower()
        if not text:
            continue
        if is_placeholder(text):
            return ""
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c))
        text = re.sub(r"[^a-z0-9\s'-]", " ", text)
        text = _WS.sub(" ", text).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


_COMPANY_SUFFIXES = (
    "inc", "inc.", "incorporated", "llc", "l.l.c.", "ltd", "ltd.", "limited",
    "corp", "corp.", "corporation", "co", "co.", "company", "plc", "gmbh",
    "pty", "pte", "bv", "nv", "sa", "ag", "llp", "lp", "the",
)


def normalize_company(name):
    """Lowercase, drop punctuation and legal suffixes.

    'Acme, Inc.' and 'Acme LLC' collapse to 'acme'. That is intentional and it
    is also the rule most likely to produce a false positive, which is why the
    company duplicate check reports it as a review queue rather than a
    confirmed duplicate.
    """
    text = clean(name).lower()
    if not text or is_placeholder(text):
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9\s&-]", " ", text)
    tokens = [t for t in _WS.sub(" ", text).strip().split(" ") if t]
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    # Leading suffixes are NOT stripped. Legal suffixes only ever trail, and
    # several entries in the list are ordinary leading words or initials:
    # stripping them merges "AG Barr" into "Barr Ltd", "LP Building Solutions"
    # into "Building Solutions Inc", and "SA Recruitment" into "Recruitment
    # Pty". Those are different companies. Only a leading article is dropped.
    if tokens and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def normalize_domain(value):
    """Bare registrable-looking host: no scheme, no www, no path, no port."""
    text = clean(value).lower()
    if not text or is_placeholder(text):
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1]          # tolerate someone pasting an email
    text = text.split(":", 1)[0]        # strip a port
    if text.startswith("www."):
        text = text[4:]
    return text.strip(" .")


def phone_format_family(value):
    """Which way a phone number was typed, for the consistency check.

    Not a quality judgement. It only reports that a portal holds the same kind
    of data in several shapes, which breaks dialers and dedupe alike.
    """
    text = clean(value)
    if not text:
        return "empty"
    if text.startswith("+"):
        return "e164ish"
    if "(" in text or ")" in text:
        return "parenthesized"
    if "-" in text:
        return "dashed"
    if "." in text:
        return "dotted"
    if " " in text:
        return "spaced"
    if text.isdigit():
        return "bare_digits"
    return "other"
