"""Deciding whether a deal is open, closed, or won.

This looks like it should be one line and is not, because the obvious source is
unreliable.

Pipeline stage metadata carries an `isClosed` flag, and the natural thing is to
trust it. But `isClosed` does not appear in HubSpot's published v3 pipeline
schema at all -- that schema documents only `probability` and `ticketState` --
and HubSpot's own documentation contains real portal audit dumps where the same
"Closed won" stage is recorded as `isClosed: "true"` in one revision and
`"false"` in a later one. The stage-creation API accepts only `probability` in
metadata, so there is no way to correct a stage whose flag is wrong.

Betting the deal checks on that flag means that on some portals every
historical won and lost deal gets reported as an open deal past its close date.
That is a report which is mostly false positives, handed to someone who was
promised every number traces to a rule.

So the order of preference is:
  1. hs_is_closed / hs_is_closed_won on the deal record itself. HubSpot
     calculates these, they are per-record, and they are what the CRM's own UI
     filters on.
  2. Pipeline metadata, as a fallback, with closed-won taken as the
     highest-probability closed stage in each pipeline rather than requiring
     exactly 1.0 -- a portal is free to put Closed Won at 0.8.
  3. Neither available: say so, and let the check report NOT_RUN.
"""

from .normalize import clean

#: Requested from the API when present, but never required. A portal without
#: them falls back to pipeline metadata.
OPTIONAL_PROPERTIES = ("hs_is_closed", "hs_is_closed_won")


def _truthy(value):
    return clean(value).lower() in {"true", "yes", "1"}


class StageResolver:
    def __init__(self, profile):
        schema = profile.schema("deals")
        self.has_closed_property = schema.has("hs_is_closed")
        self.has_won_property = schema.has("hs_is_closed_won")
        self.open_stage_ids = profile.open_stage_ids
        self.won_stage_ids = profile.closed_won_stage_ids
        self.all_stage_ids = profile.all_stage_ids

    # -- availability ------------------------------------------------------

    def open_reason(self):
        """None if open/closed can be determined, else why it cannot."""
        if self.has_closed_property or self.all_stage_ids:
            return None
        return ("this portal has neither the hs_is_closed deal property nor any "
                "pipeline stages, so open and closed deals cannot be told apart")

    def won_reason(self):
        if self.has_won_property or self.won_stage_ids:
            return None
        return ("this portal has neither the hs_is_closed_won deal property nor "
                "a closed stage in any pipeline, so won deals cannot be identified")

    @property
    def source(self):
        return "deal properties" if self.has_closed_property else "pipeline stages"

    # -- per record --------------------------------------------------------

    def is_open(self, record):
        """True, False, or None when this record cannot be classified.

        None matters: a portal that has the property but leaves it blank on a
        record, and has no pipeline stages to fall back on, cannot be judged.
        Returning False there would quietly treat every deal as closed and let
        the open-deal checks pass having examined nothing.
        """
        props = record.get("properties") or {}
        if self.has_closed_property and clean(props.get("hs_is_closed")):
            return not _truthy(props.get("hs_is_closed"))
        if not self.all_stage_ids:
            return None
        return clean(props.get("dealstage")) in self.open_stage_ids

    def is_won(self, record):
        props = record.get("properties") or {}
        if self.has_won_property and clean(props.get("hs_is_closed_won")):
            return _truthy(props.get("hs_is_closed_won"))
        if not self.all_stage_ids:
            return None
        return clean(props.get("dealstage")) in self.won_stage_ids
