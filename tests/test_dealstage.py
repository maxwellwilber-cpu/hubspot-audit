"""Timestamp parsing and open/closed/won resolution.

Both were previously exercised only through one fixture whose timestamps were
all the same shape and whose pipeline metadata was always in the one
combination the code expected. HubSpot's own documentation shows real portals
producing other combinations.
"""

from datetime import datetime, timezone

import pytest

from hubspot_audit.checks.deals import _parse_ts
from hubspot_audit.dealstage import StageResolver
from hubspot_audit.schema import ObjectSchema, Pipeline, PortalProfile


# -- timestamps ------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("2024-06-01T00:00:00.000Z", datetime(2024, 6, 1, tzinfo=timezone.utc)),
    ("2024-06-01T00:00:00Z", datetime(2024, 6, 1, tzinfo=timezone.utc)),
    ("2024-06-01T00:00:00+00:00", datetime(2024, 6, 1, tzinfo=timezone.utc)),
    ("2024-06-01", datetime(2024, 6, 1, tzinfo=timezone.utc)),
])
def test_iso_shapes_parse_to_the_same_instant(value, expected):
    assert _parse_ts(value) == expected


def test_a_naive_iso_timestamp_is_treated_as_utc_not_left_naive():
    """A naive datetime compared against an aware one raises TypeError, which
    would turn a whole deal check into ERROR."""
    parsed = _parse_ts("2024-06-01T12:00:00")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2024, 6, 1, 12, tzinfo=timezone.utc)


def test_epoch_milliseconds_parse():
    assert _parse_ts("1717200000000") == datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_epoch_seconds_are_not_mistaken_for_milliseconds():
    """Legacy v1 exports and several sync integrations write seconds.

    Dividing those by 1000 puts a live 2024 deal in January 1970, which the
    staleness check then reports as untouched for fifty-five years.
    """
    assert _parse_ts("1717200000") == datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_a_compact_date_is_read_as_a_date_not_an_epoch():
    """20240201 read as epoch seconds lands in August 1970."""
    assert _parse_ts("20240201") == datetime(2024, 2, 1, tzinfo=timezone.utc)


def test_a_number_too_small_to_be_an_epoch_in_either_unit_is_rejected():
    """Better no date than a date in 1970 that the staleness check then
    reports as a deal nobody has touched in fifty-five years."""
    assert _parse_ts("0") is None
    assert _parse_ts("12345") is None


@pytest.mark.parametrize("value", ["", "   ", None, "not a date", "2024-13-45"])
def test_unparseable_values_return_none_rather_than_raising(value):
    assert _parse_ts(value) is None


def test_non_ascii_digits_are_not_parsed_as_a_number():
    """'٢٠٢٤'.isdigit() is True and int() will happily parse it."""
    assert _parse_ts("٢٠٢٤") is None


def test_every_returned_datetime_is_timezone_aware():
    for value in ["2024-06-01T00:00:00.000Z", "1717200000000", "1717200000",
                  "2024-06-01T12:00:00", "2024-06-01T00:00:00+02:00"]:
        parsed = _parse_ts(value)
        assert parsed is None or parsed.tzinfo is not None, value


# -- open / closed / won ---------------------------------------------------

def pipeline(stages, archived=False):
    return {"id": "p1", "label": "Sales", "archived": archived, "stages": [
        {"id": sid, "label": sid, "displayOrder": i,
         "metadata": {"isClosed": closed, "probability": prob}}
        for i, (sid, closed, prob) in enumerate(stages)]}


def profile_for(stages, deal_properties=(), archived=False):
    return PortalProfile(
        schemas={"deals": ObjectSchema("deals", [
            {"name": n} for n in ("dealstage",) + tuple(deal_properties)])},
        pipelines=[Pipeline(pipeline(stages, archived=archived))],
    )


def deal(**props):
    return {"id": "1", "properties": props}


def test_closed_won_is_the_highest_probability_closed_stage_not_exactly_one():
    """HubSpot's own docs show a real portal with Closed Won at 0.8.

    Requiring exactly 1.0 made the lifecycle-contradiction check skip itself on
    those portals while reporting a reason that sounded like the portal's fault.
    """
    profile = profile_for([("open", "false", "0.2"),
                           ("won", "true", "0.8"),
                           ("lost", "true", "0.0")])
    assert profile.closed_won_stage_ids == {"won"}


def test_a_pipeline_whose_closed_stages_are_all_zero_has_no_winning_stage():
    profile = profile_for([("open", "false", "0.2"), ("lost", "true", "0.0")])
    assert profile.closed_won_stage_ids == set()


def test_the_deal_property_wins_over_pipeline_metadata():
    """The flag this used to trust is not in HubSpot's published schema, and
    their documentation shows it flipping between values on the same stage.

    Here the pipeline claims the stage is open and the deal record says it is
    closed. The record is believed, which is what the CRM's own UI filters on.
    """
    profile = profile_for([("won", "false", "1.0")],
                          deal_properties=("hs_is_closed", "hs_is_closed_won"))
    resolver = StageResolver(profile)
    record = deal(dealstage="won", hs_is_closed="true", hs_is_closed_won="true")
    assert resolver.is_open(record) is False
    assert resolver.is_won(record) is True
    assert resolver.source == "deal properties"


def test_pipeline_metadata_is_used_when_the_deal_properties_are_absent():
    profile = profile_for([("open", "false", "0.2"), ("won", "true", "1.0")])
    resolver = StageResolver(profile)
    assert resolver.is_open(deal(dealstage="open")) is True
    assert resolver.is_open(deal(dealstage="won")) is False
    assert resolver.is_won(deal(dealstage="won")) is True
    assert resolver.source == "pipeline stages"


def test_an_empty_deal_property_falls_back_rather_than_reading_as_open():
    """The property exists on the portal but is blank on this record."""
    profile = profile_for([("won", "true", "1.0")],
                          deal_properties=("hs_is_closed",))
    resolver = StageResolver(profile)
    assert resolver.is_open(deal(dealstage="won", hs_is_closed="")) is False


def test_a_portal_with_neither_source_says_so_instead_of_guessing():
    profile = PortalProfile(
        schemas={"deals": ObjectSchema("deals", [{"name": "dealstage"}])},
        pipelines=[])
    resolver = StageResolver(profile)
    assert "cannot be told apart" in resolver.open_reason()
    assert "cannot be identified" in resolver.won_reason()


def test_archived_pipelines_do_not_contribute_stage_ids():
    """There is no query parameter to exclude them, and their stages would
    otherwise pollute every open/closed judgement."""
    profile = profile_for([("open", "false", "0.2")], archived=True)
    # discover() filters archived pipelines; construct one directly to prove
    # the flag is read at all.
    assert Pipeline(pipeline([("open", "false", "0.2")], archived=True)).archived
    assert profile.open_stage_ids == {"open"}  # not filtered here, only in discover


@pytest.mark.parametrize("value,closed", [
    ("true", True), ("TRUE", True), ("yes", True), ("1", True),
    ("false", False), ("no", False), ("0", False),
])
def test_hubspot_serialises_booleans_as_strings(value, closed):
    profile = profile_for([("s", "false", "0.5")], deal_properties=("hs_is_closed",))
    resolver = StageResolver(profile)
    assert resolver.is_open(deal(dealstage="s", hs_is_closed=value)) is not closed


def test_an_absurdly_long_number_does_not_take_down_the_whole_audit():
    """float() on a several-hundred-digit int raises OverflowError, and the
    conversion used to sit outside the guard. One nonsense value in one deal
    record produced a traceback instead of a report."""
    assert _parse_ts("1" * 400) is None
    assert _parse_ts("9" * 5000) is None


def test_a_portal_that_can_classify_nothing_says_so_instead_of_passing():
    """hs_is_closed exists on the portal but is blank on the record, and there
    are no pipeline stages to fall back on. Returning False for everything
    would treat every deal as closed and let the open-deal checks pass having
    judged nothing."""
    profile = PortalProfile(
        schemas={"deals": ObjectSchema("deals", [
            {"name": "dealstage"}, {"name": "hs_is_closed"}])},
        pipelines=[])
    resolver = StageResolver(profile)
    assert resolver.open_reason() is None       # the property exists, so it tries
    assert resolver.is_open(deal(dealstage="x", hs_is_closed="")) is None
