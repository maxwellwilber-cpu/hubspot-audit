# hubspot-audit

[![tests](https://github.com/maxwellwilber-cpu/hubspot-audit/actions/workflows/tests.yml/badge.svg)](https://github.com/maxwellwilber-cpu/hubspot-audit/actions/workflows/tests.yml)

Point it at a HubSpot portal, get back a list of what is broken in the CRM,
with the rule behind every number and the record IDs to fix.

```bash
git clone https://github.com/maxwellwilber-cpu/hubspot-audit
cd hubspot-audit
python3 examples/demo.py
```

That runs the whole audit against a synthetic portal served over real HTTP. No
HubSpot account needed to see what it does.

On macOS use `python3` and `pip3`. Python 3.9 or newer.

---

## It cannot write to your CRM

This is the part that matters if you are being asked to hand over a token.

The client only knows how to issue GET requests, and the private app only needs
read scopes. That claim is enforced rather than asserted:

```bash
python3 -m pytest tests/test_readonly.py -v
```

Ten tests. Eight read the shipped source and fail the build on a write verb, a
non-GET `Request`, a call that carries a request body, a reassignment of
`.method` or `.data`, a method name containing `create`/`update`/`upsert`/etc,
or an import of any third-party HTTP library. The ninth runs a complete audit
against a portal that records the HTTP method of every request it receives and
asserts nothing but GET arrives. The tenth sends a real POST at that portal to
prove the ninth would actually notice one.

That last pair exists because the static checks alone were not enough. Two
working bypasses got through an earlier version of this file: `urlopen(url,
data=...)` is a POST that constructs no `Request` node and contains no verb
literal, and a name check using exact string equality let `create_contact` and
`upsert` straight through. Both now fail the build.

Standard library only. No `requests`, no `hubspot-api-client`, nothing to
install. `pytest` is needed to run the tests and for nothing else.

---

## What it checks

25 distinct rules, instantiated as 27 checks (the dead-custom-property rule
runs once per object type) across contacts, companies, deals, the
relationships between them, and the schema itself.

```bash
python3 -c "
from hubspot_audit.audit import build_checks
from hubspot_audit.schema import ObjectSchema, PortalProfile
p = PortalProfile(schemas={t: ObjectSchema(t, []) for t in ('contacts','companies','deals')})
c = build_checks(p)
print(len(c), 'checks,', len({type(x).__name__ for x in c}), 'distinct rules')"
```

The ordinary ones: duplicate contacts by email, duplicate companies by domain,
missing or malformed email addresses, placeholder addresses, records with no
owner, phone numbers stored in four different formats, open deals past their
close date, open deals with no amount, open deals nobody has touched in 90 days.

The ones that need to know how a business runs:

- **A contact with a closed-won deal who is not marked a customer.** They
  bought, and the CRM still says they did not, so they stay in lead nurture and
  every conversion rate computed from lifecycle stage is wrong.
- **Deals linked to no contact.** Usually created by an import or an
  integration rather than a rep. Nobody knows who to call.
- **Companies with no contacts, and contacts with no company.** Account-level
  reporting silently omits both.
- **Custom properties that exist on every record and are populated on none.**
  The archaeology of somebody's 2023 campaign, still cluttering every form.

---

## Every number traces to a rule

Real output, unedited:

```
  6 contacts share 3 email addresses
      rule: exact match on lowercased, trimmed email
      ids:  1, 2, 15, 17, 16 (+1 more)

  1 contact has a won deal but is not marked as a customer
      rule: contact is associated with a deal the portal reports as closed-won (via pipeline stages), while the contact's lifecyclestage is not 'customer'
      ids:  14
```

"Your CRM has 340 problems" is a sales pitch. "340 contacts share 118 email
addresses, matched on exact lowercased email, here are the IDs" is a work
order. `--csv` writes one row per affected record so somebody can act on it.

The headline count is distinct records, not findings. A contact with a missing
email *and* no owner is one dirty record, not two.

---

## Checks that could not run say so

A check that examined nothing reports `NOT_RUN` with a reason, and the reason
is printed. It never reports `PASS`.

```
  NOT RUN  (2 checks)
  Custom companies properties that are never populated
      this portal has no custom companies properties
```

Same for the whole run: if the token is rejected, or no object can be read, the
tool exits with an error rather than rendering a report in which every check
was skipped. Someone handed that report would read "no findings" as good news.
Discovering nothing and discovering nothing wrong must never look the same.

`--max-records` follows the same rule. It makes a large portal fast to sample,
every output carries a partial-scan banner, and the checks that join two object
types are skipped with a stated reason, because truncating contacts while
reading every company would otherwise report companies as orphaned when their
contacts were simply never read.

---

## Measured against planted defects

`tests/planted.py` builds a portal with 25 deliberate defects, each mapped both
to the check that should catch it and to the exact record IDs it should report.

```bash
python3 examples/demo.py
```

```
Planted defects: 25
Caught exactly:  25
Missed:          0
```

Worth being precise about what that proves. The expectation list is written
alongside the code, so it measures whether the rules do what they claim, not
whether they are the right rules. What it does catch is a rule that silently
widens or narrows: the suite asserts the exact IDs, and asserts that no check
outside the planted set fires at all.

Every check is also proved capable of passing, against a second portal with no
defects. A rule that can only ever fire is not a check.

```bash
python3 -m pytest tests/ -q
```

215 tests, covering the checks, the normalization rules, timestamp parsing,
pagination and retry behaviour, the report arithmetic, the read-only
guarantee, and a 50,000-contact portal.

---

## Built for what a real portal returns

Run against a live HubSpot portal on 2026-09-14: 27 checks, 8 API requests, no
errors. Two assumptions turned out to be wrong before that run, both since
fixed, and one of them is a case where HubSpot's own documentation contradicts
itself:

- **A requested property with no value comes back as an explicit `null`, not as
  an absent key.** The v3 object guides say null; the current object-APIs guide
  says it will not appear at all. It is null. The test fake now matches.
- **Real pipeline metadata serialises both `isClosed` and `probability` as
  strings**, so `"1.0"` rather than `1.0`.

Confirmed as expected: `hs_is_closed` and `hs_is_closed_won` both exist on
deals, so the per-record path is the one that runs rather than the fallback;
lifecycle stages include the internal `customer` value; and the owners endpoint
returns no paging block for a single-owner portal.

The things that break CRM integrations, and what this does about them:

| Problem | Handling |
|---|---|
| Every portal has different properties | Schema is discovered first; checks declare what they need and skip with a reason |
| Deal stages are renamed and reordered per portal | Resolved from the portal's own data, never from stage names (see below) |
| Lifecycle stages can be replaced entirely | The lifecycle check uses HubSpot's internal `customer` value and skips with a reason when a portal has no such stage |
| Free tiers have no deals object | A contacts-only portal still runs 11 of its 13 contact-object checks; the two that need companies or deals to exist say so |
| 100 requests per 10 seconds | Paced proactively, then backed off on 429, using HubSpot's own interval header |
| A daily quota is not a burst limit | Fails immediately instead of sleeping through the retry budget |
| Cursor pagination can loop | A repeated cursor terminates the scan |
| Timestamps arrive as ISO, epoch millis, epoch seconds or `YYYYMMDD` | Parsed by magnitude, so a live 2024 deal does not land in 1970 |
| A portal where nothing can be classified | Checks that could not make their judgement report NOT_RUN, never a clean pass |
| 50,000 contacts | One pass per object type feeds all 27 checks; duplicate detection is hash-keyed, not pairwise |

The list endpoints return only a small default set of properties unless you ask
for more, which is the quiet way an audit ends up validating blank fields. Each
check declares the properties it reads, the runner requests exactly that union,
the test fake refuses to return anything that was not requested, and a test
asserts the request set equals the declared set.

A 30-record portal costs 8 API requests. One pass per object type, not one per
check.

### Why deal stages are not read from `isClosed`

The obvious way to tell an open deal from a closed one is the `isClosed` flag
in pipeline stage metadata. This does not use it as the primary source, for
two reasons: `isClosed` does not appear in HubSpot's published v3 pipeline
schema, which documents only `probability` and `ticketState`; and HubSpot's own
documentation contains real portal audit dumps where the same "Closed won"
stage is recorded as `"true"` in one revision and `"false"` in a later one.

Trusting it means that on some portals every historical won and lost deal gets
reported as an open deal past its close date. So the order is: the
`hs_is_closed` and `hs_is_closed_won` properties on the deal record itself,
then pipeline metadata as a fallback, then `NOT_RUN` with a reason. Closed-won
falls back to the highest-probability closed stage rather than requiring
exactly 1.0, because a portal is free to put Closed Won at 0.8 and HubSpot's
docs show one that does.

---

## Running it against your own portal

You need a HubSpot **Service Key** with read scopes only.

Service Keys are HubSpot's current credential for single-account API access.
Legacy private app creation is being disabled on 28 September 2026 for new
accounts and 26 October 2026 for existing ones, so any guide still telling you
to create a private app is about to stop working. Existing private app tokens
keep working and this tool accepts either.

1. In HubSpot: **Settings → Integrations → Service Keys → Create service key**
2. Name it something like `CRM audit (read only)`
3. Add only these scopes, read and not write:
   - `crm.objects.contacts.read`
   - `crm.objects.companies.read`
   - `crm.objects.deals.read`
   - `crm.objects.owners.read`
4. Create it and copy the key

It authenticates as a bearer token against the same v3 endpoints, so nothing
else changes.

Then:

```bash
export HUBSPOT_TOKEN="pat-na1-..."
python3 -m hubspot_audit --markdown report.md --csv fix.csv
```

Use the environment variable rather than `--token` so the token stays out of
your shell history.

Exit code is 0 when nothing was found, 1 when something was, so it can gate a
scheduled job.

---

## Layout

```
hubspot_audit/
  client.py      GET-only HTTP, cursor pagination, rate limiting, typed errors
  schema.py      portal discovery: properties, pipelines, owners
  dealstage.py   open / closed / won, and why it is not one line
  normalize.py   conservative email, phone, name and domain normalization
  checks/        the rules, as streaming collectors
  audit.py       one pass per object type, feeding every check
  report.py      terminal, markdown, CSV and JSON output
tests/
  fake_portal.py a HubSpot v3 API served over real HTTP on localhost
  planted.py     one portal with 25 deliberate defects, one with none
```

Normalization is deliberately conservative, because a missed duplicate costs a
client some tidying and a wrong merge destroys data in their CRM. Gmail dots
and `+tags` are **not** stripped, because that rule is true of Gmail's routing
and false as a statement about identity. Legal suffixes are stripped from the end
of a company name but never the front, since "AG Barr" and "Barr Ltd" are not
the same company. The weaker rules, matching companies by name and contacts by
name and phone, are reported as a review queue rather than as confirmed
duplicates, and the finding says so. `tests/test_normalize.py` pins each of
these.

---

## Limits

- Contacts, companies and deals. Tickets and custom objects are not covered.
- Custom property sampling is capped at 40 per object type; the finding says
  when it sampled rather than scanning everything.
- A record's associated IDs are capped at 100 per type by the API. That cannot
  produce a false "no linked contact", but a won deal with more than 100
  contacts contributes only its first 100 to the lifecycle check.
- Phone plausibility is a digit-count check. Real validation needs the country,
  which the CRM usually does not store.
- The duplicate rules are exact-match after normalization. There is no fuzzy
  matching, on purpose: a review queue somebody trusts beats a merge suggestion
  they do not.
- The live run so far covered a portal with contacts and companies but no deal
  records, so the deal checks are still exercised only against the fake.

MIT licensed.
