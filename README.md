# Hybrid Joiner

Automates new-employee account provisioning across on-prem Active
Directory and Microsoft Entra ID / Microsoft 365 — the "day one" problem
that a lot of mid-size companies running hybrid identity still solve by
hand: someone in IT gets a new-starter form, manually runs `New-ADUser`,
waits for Azure AD Connect to sync, then manually adds Entra groups and
assigns a license. It's slow, it's easy to get wrong under time pressure,
and it doesn't scale past a handful of joiners a week.

This project turns that into: drag a form onto a small desktop tool, click
Create User, and everything else — AD account creation, group cloning from
an existing colleague, Entra sync wait, conditional group assignment,
license assignment — happens on its own, with a live status log the whole
way through.

I originally built a one-off version of this as a single PowerShell script
for a hybrid AD/Entra environment I was managing. It worked, but it was
tied to one company's directory layout and one person's laptop. This repo
is a from-scratch rebuild designed to actually be deployed more than once:
a proper client/server split, an on-prem agent instead of a script someone
has to remember to run, and a config schema instead of hardcoded values.

## The problem with "just call Microsoft Graph from anywhere"

A cloud-hosted backend can't reach into a company's private on-prem Active
Directory — there's no route in, and there shouldn't be one. Opening an
inbound port from the internet to a domain controller is not a thing any
sane AD admin signs off on.

The products that actually solve this for real — Azure AD Connect,
Okta's on-prem agents — all use the same trick: a small agent installed
*inside* the network makes outbound-only connections to the cloud side,
polls for work, and does the on-prem part locally where it already has
line-of-sight to AD. No inbound firewall rule, ever.

That's the shape this project copies:

```
 ┌──────────────────────────┐        outbound HTTPS only        ┌──────────────────────────┐
 │   Customer's network     │ ─────────────────────────────────▶│   Backend                │
 │                          │◀───────────────────────────────── │   (FastAPI + Postgres)   │
 │  Connector-Agent.ps1     │      poll for jobs / post status   │  - config                │
 │  - has the AD module     │                                    │  - job queue             │
 │  - runs as a scheduled   │                                    │  - Graph/365 calls       │
 │    task, always on       │                                    │    (no on-prem hop       │
 │  - talks to local AD     │                                    │    needed for these)     │
 │    directly              │                                    └──────────────────────────┘
 └──────────────────────────┘                                              ▲
                                                                            │ HTTPS
                                                                   ┌────────────────┐
                                                                   │  Admin GUI      │
                                                                   │  (WinForms,     │
                                                                   │  client/)       │
                                                                   └────────────────┘
```

Anything that's pure Microsoft Graph — group membership, license
assignment, setting a usage location — runs straight from the backend
using an app-only (client credentials) Entra app registration. No human,
no interactive sign-in, no on-prem hop needed. Only the parts that
actually touch AD (`New-ADUser`, on-prem group cloning) get relayed to the
connector.

## How a joiner actually goes through the system

1. An admin drags the joiner Excel form onto the GUI. It's uploaded to
   `POST /preview-excel`, which reads the cells named in that deployment's
   `excel_field_map` and shows back a "detected joiner: X" preview —
   nothing is created yet.
2. The admin checks/edits the suggested logon name and clicks **Create
   User**. The GUI posts the parsed fields to `POST /jobs`, which creates
   a job in `pending` status.
3. The connector, polling every 30 seconds, claims it (`POST
   /connector/jobs/claim`) — this is an atomic database update, so two
   connector instances (or a retried poll) can never both grab the same
   job.
4. It resolves the target OU and email domain by looking up the "similar
   colleague" named on the form first, and only falls back to a
   site/company lookup table if that colleague can't be found — a real
   account's actual setup is more trustworthy than a static table that
   goes stale.
5. It checks for a UPN/SamAccountName collision, runs `New-ADUser`, clones
   the colleague's group memberships (minus anything matching an excluded
   pattern, and minus the line-manager group), adds the line-manager group
   if the form flagged them as one, and optionally kicks off a delta AD
   sync.
6. It reports back (`POST /connector/jobs/{id}/onprem-complete`) and
   returns immediately — it does **not** sit around waiting for Entra
   sync.
7. That hands off to a background task on the backend, which polls
   Microsoft Graph for up to 15 minutes for the account to show up, then
   evaluates every configured group rule against the form data, adds
   matching Entra groups, sets the usage location, and assigns licenses.
   Every step is soft-failure — one missing group doesn't block licensing.
8. The GUI polls job status every few seconds and renders the whole thing
   as a colored, timestamped log — green for success, orange for warnings,
   red for failures — so the person running it can see exactly what
   happened without digging through anything.

## Why one deployment per customer, not shared multi-tenancy

There's no "create a tenant" API here. A second customer gets a second,
completely separate deployment of the same code — its own backend, own
database, own Entra app registration, own config. That costs more per
customer than a shared multi-tenant backend would, but it buys something
worth more at this scale: one customer's bug, misconfiguration, or breach
structurally cannot touch another's, because there's no shared state
between them to get wrong in the first place.

## What's configurable vs. what's code

Everything specific to a company — OU paths, which groups map to which
form fields, license SKUs, the Excel cell layout — lives in one config
object, set through the API (`PUT /config`), not hardcoded anywhere. See
[`config/config.example.yaml`](config/config.example.yaml) for the shape.
A group rule's `condition` is one of a small fixed set (`always`,
`is_line_manager`, `has_mobile_device`, `field:X`,
`field_matches:X:regex`, `field_not_matches:X:regex`), which covers
everything from "give everyone this group" to "pick group A or B based on
department" without needing actual code changes per customer.

## Getting it running

**Fastest way to see it work — no Windows machine, no Active Directory,
no cost:**

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -v
```

That runs the full test suite (see "Testing" below) entirely offline.

**To actually deploy it against a real (or trial) tenant:**
[`GETTING_STARTED.md`](GETTING_STARTED.md) walks through it step by step —
Entra app registration, Azure deployment, loading config, installing the
connector, setting up the admin GUI. If you don't have a real AD/Entra
environment to test against yet,
[`TEST_AUTH_AND_LOGIC.md`](TEST_AUTH_AND_LOGIC.md) and
[`TEST_AD_SETUP.md`](TEST_AD_SETUP.md) cover standing up a free trial
tenant and a throwaway test AD forest respectively.

## Layout

- **`backend/`** — the FastAPI app. One deployment per customer.
  - `routes/admin.py` — operator-only routes (behind a root token or a
    named admin key): set config and the Graph credential, issue/revoke
    API keys, read the audit log.
  - `routes/jobs.py` — parse/preview a joiner form, create a job, let the
    connector claim/log/complete it, check status.
  - `graph_actions.py` — the cloud-only provisioning steps, run as a
    background task once the connector reports the AD account exists.
  - `secrets_backend.py` — routes the Graph client secret through Azure
    Key Vault in production, plaintext only for local dev.
  - `recovery.py` / `rate_limit.py` / `retention.py` / `security_headers.py`
    — the hardening pass described below.
  - `tests/` — the pytest suite.
- **`connector/`** — `Connector-Agent.ps1`, the on-prem polling agent, and
  a script to register it as a self-healing Windows Scheduled Task.
- **`client/`** — the WinForms admin GUI, dark-mode aware, and its config
  template.
- **`scripts/configure_backend.py`** — one-time setup script: loads a
  config YAML into a freshly deployed backend and issues its keys.
- **`config/config.example.yaml`** — the shape of a deployment's config,
  all placeholder values.

## Where this stands honestly

This has been built and tested rigorously, but entirely offline — I don't
currently have a spare Windows domain controller or a production Entra
tenant to point it at. Two things are true at once:

- The logic — auth boundaries, job state machine, group rule evaluation,
  concurrency safety, error recovery — has real, repeatable test coverage
  and I've deliberately broken things to confirm the tests actually catch
  regressions, not just pass by coincidence (see below).
- `Connector-Agent.ps1`'s actual `New-ADUser`/`Add-ADGroupMember` calls,
  and `graph_actions.py`'s calls against a real Microsoft Graph tenant,
  have only been checked for correct PowerShell syntax and mocked HTTP
  behavior respectively — not run against the real thing yet. That's the
  next step, not a hidden gap: `TEST_AUTH_AND_LOGIC.md` and
  `TEST_AD_SETUP.md` exist specifically to close it cheaply.

## Testing

```bash
cd backend
.venv/bin/pytest -v
```

73 tests, runs in about a second, completely offline — no Postgres, no
Azure, no live Entra tenant. A few things make that possible:

- Every test gets a fresh, throwaway file-based SQLite database (not a
  shared in-memory one — more on why below).
- Every Microsoft Graph HTTP call is intercepted with
  [`respx`](https://github.com/lundberg/respx) and answered with a canned
  response, so the real request-building and response-parsing code runs,
  only the network call itself is faked.
- The 15-minute Entra sync wait is monkeypatched down to near-zero for the
  tests that exercise the retry/timeout path.

It covers: auth boundaries between all four credential types (a client
key can't reach connector routes and vice versa, a revoked key stops
working on its very next request, admin actions are attributed by name in
the audit log); the full job lifecycle including atomic claiming; every
group-rule condition type against realistic inputs; the cloud-provisioning
flow against mocked Graph responses (success, bad credentials, a group
that doesn't exist, a user that never syncs); the interrupted-job recovery
path; the auth-failure lockout; and job retention.

### Bugs this testing process actually found

I'm including this section because "I wrote tests" is a much weaker claim
than "here's what the tests caught." All of these were found and fixed
during development, not left as known issues:

**A concurrency bug that caused a real, intermittent segfault.** The
original test setup used an in-memory SQLite database shared across
threads via `StaticPool` + `check_same_thread=False`. That combination
disables Python's same-thread safety *check* without making concurrent
use of the single underlying connection actually safe. Once a background
recovery sweep started running on its own thread alongside the test
thread, that stopped being theoretical — running the suite with
`-W error::RuntimeWarning` reproduced a segfault 5 out of 5 times, while a
normal run passed cleanly every time, which is exactly what makes this
class of bug dangerous. Fixed by giving every test its own file-based
SQLite database instead, so each thread gets a real connection from a
normal pool — also a closer match to how Postgres behaves in production,
where this bug class doesn't exist in the first place.

**A silent data-loss bug in API responses.** A couple of routes wrote an
audit log entry (a second `session.commit()`) right after refreshing the
object they were about to return. That second commit silently expired the
already-refreshed SQLAlchemy object, so the route serialized an empty `{}`
instead of the real record — no error, just wrong output. It happened
twice, in two different routes, before I generalized the fix (always
commit everything, *then* refresh, *then* return) and added a regression
test that pins the exact field value expected back.

**A timezone-comparison crash in the concurrency-safe job claim.**
SQLAlchemy's default bulk-`UPDATE` strategy re-evaluates the `WHERE`
clause in Python against whatever the database handed back — and SQLite
round-trips a timezone-aware datetime as naive, so comparing it against an
aware cutoff value raised `TypeError: can't compare offset-naive and
offset-aware datetimes`. Fixed with `synchronize_session=False`, since the
caller re-fetches the row fresh immediately after anyway.

**A rate-limiter bug that would have been a production incident.** The
auth-failure lockout originally keyed failed attempts off
`request.client.host`. That's fine on a raw socket, but behind a reverse
proxy (which is how this is actually deployed — Azure App Service, or
anything similar) that value is the *proxy's own address*, identical for
every real visitor. Left as-is, one person mistyping their API key
repeatedly would have locked out every legitimate user simultaneously.
Found via a test-isolation bug (the lockout's state was accidentally
shared across unrelated tests because it lived on a middleware object
Starlette caches as a singleton) that led me to look harder at how the
"source" was actually being identified. Fixed by keying off
`X-Forwarded-For` instead, and moving the state somewhere a test can
actually reset it between runs.

**A silent wildcard bug in the admin GUI's log coloring.** The status log
colored lines by checking `-like "*[FAIL]*"` in PowerShell. `[` and `]`
are character-class syntax to `-like`, not literal brackets — so that
pattern actually matched almost any line containing the letters F, A, I,
or L, not specifically the text `[FAIL]`. Switched to a plain `.Contains()`
check.

**A silent job-abandonment gap.** If the on-prem connector claimed a job
and then hit a genuinely unexpected error — not the specific `New-ADUser`
failure it already handles, something nobody anticipated — its outer
error handler logged a warning and moved on, leaving the job claimed
forever with no failure ever reported. Nothing on the connector side can
notice this about itself; only the backend, watching how long a job has
sat claimed, can. Fixed with a periodic sweep that resets anything claimed
for more than 20 minutes back to pending — safe to retry, because the
connector's own duplicate-account check means a retry against an account
a dead attempt already finished creating just fails cleanly with "already
exists" instead of creating a duplicate.

In each case I reverted the fix, confirmed the relevant test actually
failed without it, then restored the fix and confirmed it passed again —
a passing test suite that would also pass with the bug back in place
isn't proving anything.

## Stack

FastAPI, SQLModel/SQLAlchemy, Postgres (SQLite for local dev/tests),
Microsoft Graph (app-only auth), Azure Key Vault, Docker, Azure App
Service. The on-prem and admin-facing pieces are PowerShell/WinForms,
targeting the PowerShell 5.1 most Windows admin machines still ship with
by default rather than requiring PowerShell 7.

## License

MIT — see [`LICENSE`](LICENSE).
