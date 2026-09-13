# What I need from you to finish this

Two things, both quick.

## 1. Push the repo

The folder is a git repo with two commits already. Create an empty repo on
GitHub named `hubspot-audit` (no README, no .gitignore, no licence, or the
push will conflict), then:

```bash
cd ~/AI_Career_Development/07-github/hubspot-audit
git remote add origin https://github.com/maxwellwilber-cpu/hubspot-audit.git
git branch -M main
git push -u origin main
```

Then set the description and topics so it matches your other repos:

```bash
gh repo edit maxwellwilber-cpu/hubspot-audit \
  --description "Read-only CRM hygiene audit for HubSpot. 25 rules, every finding traced to the rule that produced it." \
  --homepage "https://github.com/maxwellwilber-cpu/hubspot-audit" \
  --add-topic python --add-topic hubspot --add-topic crm \
  --add-topic data-quality --add-topic api-integration
```

## 2. A HubSpot developer test account and a read-only token

I cannot create accounts, so this part is yours. It takes about five minutes
and it is the last thing standing between this repo and a claim that it has
been validated against a real portal.

1. Go to developers.hubspot.com and create a free developer account
2. Inside it, create a **test account** (developer accounts have a menu for
   this; a test account comes pre-populated with sample contacts and deals)
3. In that test account: **Settings > Integrations > Private Apps > Create a
   private app**
4. Name it `CRM audit (read only)`
5. On the **Scopes** tab tick only these four:
   - `crm.objects.contacts.read`
   - `crm.objects.companies.read`
   - `crm.objects.deals.read`
   - `crm.objects.owners.read`
6. Create it, copy the access token, and paste it back to me

Then I run:

```bash
export HUBSPOT_TOKEN="pat-na1-..."
python3 -m hubspot_audit --markdown report.md --csv fix.csv
```

## What that run is actually for

Everything in this repo has been tested against a local server that implements
HubSpot's documented v3 contract. That is not the same as having run against a
live portal, and the README says so in plain words rather than implying
otherwise.

The specific things a live run would confirm or correct:

- the exact shape of the `/crm/v3/properties/{type}` response, which I
  assembled from HubSpot's documented field list rather than a published
  literal example
- whether `hs_is_closed` and `hs_is_closed_won` are actually present on deals
  in a normal portal (the whole open/closed judgement prefers them)
- what `metadata.isClosed` and `probability` really contain on a live pipeline
- whether a property requested but unset comes back as null or is simply absent

If any of those differ, the fix is small and the tests already exist to pin it.
Once it has run clean against a real portal, one line of the README changes and
the repo stops carrying a caveat.

## Using it as a sales opener

The reason to build this rather than a generic connector demo: you can run it
free against a prospect's portal and hand them the list of what is broken, with
counts and record IDs. That is a conversation about their business, not a bid
against forty other freelancers.

Two things to be careful about when you do:

- Ask them to create the token themselves with the four scopes above. Never
  ask for their password, and never accept a token with write scopes on it.
  The read-only guarantee is the reason they will say yes; behave like it
  matters.
- Send the markdown report, not the CSV, on first contact. The CSV is the work
  order and it comes after they have agreed to the cleanup.
