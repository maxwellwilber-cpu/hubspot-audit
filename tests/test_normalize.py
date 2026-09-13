"""Normalization rules, tested directly.

These functions decide which records get merged, so the README makes explicit
promises about what they do and do not collapse. Every one of those promises is
pinned here. Before these existed, rewriting normalize_email to strip Gmail
dots and +tags -- exactly the behaviour the README says is absent -- left the
whole suite green.

The asymmetry that shapes all of it: a missed duplicate costs a client some
tidying, and a wrong merge destroys data in their CRM. When the two trade off,
these rules take the missed duplicate.
"""

import pytest

from hubspot_audit.normalize import (
    clean, email_domain, email_is_placeholder, email_is_role_account,
    email_is_wellformed, is_placeholder, normalize_company, normalize_domain,
    normalize_email, normalize_name, normalize_phone, phone_format_family,
    phone_is_plausible,
)


# -- email -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Ada@Acme.TEST", "ada@acme.test"),
    ("  ada@acme.test  ", "ada@acme.test"),
    ("ada@acme.test,", "ada@acme.test"),
    ("Ada Lovelace <ada@acme.test>", "ada@acme.test"),
    ("", ""),
])
def test_email_normalization_collapses_only_cosmetic_differences(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("a,b", [
    # Gmail routes all of these to one inbox. They are still NOT merged, because
    # "the mail arrives in the same place" is not the same claim as "these are
    # the same person", and acting on it merges people who deliberately use
    # tagged addresses to stay separate.
    ("ada.byron@gmail.com", "adabyron@gmail.com"),
    ("ada+crm@gmail.com", "ada@gmail.com"),
    ("ada@googlemail.com", "ada@gmail.com"),
    # Different people at the same company.
    ("ada@acme.test", "grace@acme.test"),
])
def test_addresses_that_must_never_be_treated_as_the_same_person(a, b):
    assert normalize_email(a) != normalize_email(b)


@pytest.mark.parametrize("value,ok", [
    ("ada@acme.test", True),
    ("ada@sub.acme.co.uk", True),
    ("ada@", False),
    ("@acme.test", False),
    ("ada.acme.test", False),
    ("ada@acme", False),          # no TLD
    ("ada acme@test.com", False), # embedded space
    ("", False),
])
def test_email_shape_detection(value, ok):
    assert email_is_wellformed(value) is ok


def test_email_domain_extraction():
    assert email_domain("Ada@Acme.TEST") == "acme.test"
    assert email_domain("not-an-email") == ""


@pytest.mark.parametrize("value,is_role", [
    ("info@acme.test", True),
    ("sales@acme.test", True),
    ("sales+webform@acme.test", True),   # the +tag must not hide it
    ("SUPPORT@ACME.TEST", True),
    ("ada@acme.test", False),
    ("salesperson@acme.test", False),    # substring, not a role account
    ("not-an-email", False),
])
def test_role_account_detection(value, is_role):
    assert email_is_role_account(value) is is_role


@pytest.mark.parametrize("value,placeholder", [
    ("test@test.com", True),
    ("anything@example.com", True),
    ("ada@acme.test", False),
    ("", False),
])
def test_placeholder_email_detection(value, placeholder):
    assert email_is_placeholder(value) is placeholder


# -- phone -----------------------------------------------------------------

def test_the_same_number_typed_four_ways_compares_equal():
    """The docstring's own claim, which nothing used to test."""
    variants = ["+1 (555) 010-9999", "555-010-9999", "5550109999", "1-555-010-9999"]
    normalized = {normalize_phone(v) for v in variants}
    assert normalized == {"5550109999"}


def test_a_leading_one_is_only_stripped_when_it_is_a_country_code():
    # 11 digits starting with 1: US country code, stripped.
    assert normalize_phone("1-555-010-9999") == "5550109999"
    # 10 digits starting with 1: an area code, kept.
    assert normalize_phone("155-501-0999") == "1555010999"


@pytest.mark.parametrize("value,plausible", [
    ("5550109999", True),
    ("+44 20 7946 0958", True),
    ("123", False),
    ("", False),
    ("1234567890123456", False),   # 16 digits
])
def test_phone_plausibility_is_a_length_check_only(value, plausible):
    assert phone_is_plausible(value) is plausible


@pytest.mark.parametrize("value,family", [
    ("+1 555 010 9999", "e164ish"),
    ("(555) 010-9999", "parenthesized"),
    ("555-010-9999", "dashed"),
    ("555.010.9999", "dotted"),
    ("555 010 9999", "spaced"),
    ("5550109999", "bare_digits"),
    ("", "empty"),
])
def test_phone_format_families(value, family):
    assert phone_format_family(value) == family


# -- names -----------------------------------------------------------------

def test_accents_and_punctuation_fold_but_distinct_names_stay_distinct():
    assert normalize_name("José", "Álvarez") == normalize_name("Jose", "Alvarez")
    assert normalize_name("Mary-Jane", "O'Neill") == "mary-jane o'neill"
    assert normalize_name("Ada", "Byron") != normalize_name("Ada", "Lovelace")


@pytest.mark.parametrize("first,last", [
    ("Grace", "Na"),      # "na" is in the placeholder list and a real surname
    ("Grace", "X"),
    ("Grace", "N/A"),
    ("Grace", "unknown"),
])
def test_a_placeholder_name_piece_voids_the_whole_key(first, last):
    """Dropping just the bad half leaves a one-token key.

    "Grace Na", "Grace X" and a surname-less "Grace" would otherwise all
    normalize to 'grace' and, on a shared office line, be reported as one
    person entered three times.
    """
    assert normalize_name(first, last) == ""
    assert normalize_name(first, last) != normalize_name("Grace", "Hopper")


def test_a_missing_name_piece_is_not_the_same_as_a_placeholder_one():
    # Genuinely absent is allowed to produce a single token; the duplicate check
    # is what decides whether that is enough evidence.
    assert normalize_name("Grace", "") == "grace"


# -- companies -------------------------------------------------------------

def test_trailing_legal_suffixes_are_stripped():
    assert normalize_company("Acme, Inc.") == normalize_company("Acme LLC") == "acme"
    assert normalize_company("The Acme Company") == "acme"


@pytest.mark.parametrize("a,b", [
    # Every one of these was merged when leading legal suffixes were stripped.
    ("AG Barr", "Barr Ltd"),
    ("LP Building Solutions", "Building Solutions Inc"),
    ("SA Recruitment", "Recruitment Pty"),
    ("Corporation Service Company", "Service Co"),
    ("CO Logistics", "Logistics Limited"),
])
def test_leading_words_that_look_like_legal_suffixes_are_not_stripped(a, b):
    """Legal suffixes only ever trail. Stripping them from the front merges
    unrelated companies, which is the worst outcome this package has."""
    assert normalize_company(a) != normalize_company(b)


def test_company_placeholders_produce_no_key():
    assert normalize_company("N/A") == ""
    assert normalize_company("") == ""


# -- domains ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://www.acme.test/pricing?a=1", "acme.test"),
    ("HTTP://ACME.TEST", "acme.test"),
    ("www.acme.test", "acme.test"),
    ("acme.test:8080", "acme.test"),
    ("ada@acme.test", "acme.test"),     # somebody pasted an email
    ("acme.test.", "acme.test"),
    ("n/a", ""),
])
def test_domain_normalization(raw, expected):
    assert normalize_domain(raw) == expected


def test_different_domains_stay_different():
    assert normalize_domain("acme.test") != normalize_domain("acme.co.test")


# -- shared helpers --------------------------------------------------------

def test_clean_collapses_whitespace_including_non_breaking_spaces():
    assert clean("  Ada  Byron \n") == "Ada Byron"
    assert clean(None) == ""


@pytest.mark.parametrize("value,placeholder", [
    ("n/a", True), ("N/A ", True), ("unknown", True), ("--", True),
    ("Ada", False), ("Nathan", False),
])
def test_placeholder_detection(value, placeholder):
    assert is_placeholder(value) is placeholder
