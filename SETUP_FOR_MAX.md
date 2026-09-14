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

## 2. Done: validated against a live portal

Ran clean on 2026-09-14. 27 checks, 8 API requests, no errors.

Two assumptions were wrong and are now fixed:

- A requested property with no value comes back as an explicit `null`, not as
  an absent key. HubSpot's own docs contradict themselves on this; the live API
  settled it.
- Real pipeline metadata serialises `isClosed` and `probability` as strings.

Confirmed as expected: `hs_is_closed` and `hs_is_closed_won` both exist on
deals, lifecycle stages include the internal `customer` value, and the owners
endpoint omits the paging block for a single-owner portal.

One gap left: the portal had contacts and companies but no deals, so the five
deal checks and the two deal-association checks are still exercised only
against the fake. Adding two or three deals in the UI and re-running would
close it, and takes about two minutes.

### Credential note, worth knowing before you ask a client

HubSpot is retiring legacy private apps. Creation is disabled 28 September 2026
for new accounts and 26 October 2026 for existing ones. The replacement is a
**Service Key**: Settings > Integrations > Service Keys > Create service key,
same four read scopes, same bearer-token authentication.

The README now documents Service Keys. Any guide still telling people to create
a private app is weeks from being wrong, which is worth remembering if you send
a client setup instructions you found somewhere else.

### Delete the key when you are done

Settings > Integrations > Service Keys, delete `CRM audit (read only)`. It has
been pasted into a chat log and there is no reason to leave it live. Making a
new one takes thirty seconds.

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
