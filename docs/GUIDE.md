# The Complete Guide to cyberfox-into-ninja

*From zero knowledge to expert. No prior experience with APIs, AutoElevate, or
NinjaOne assumed.*

---

## Table of contents

1. [What this project does, in one paragraph](#1-what-this-project-does-in-one-paragraph)
2. [The world this lives in](#2-the-world-this-lives-in)
3. [Crash course: the technology concepts](#3-crash-course-the-technology-concepts)
4. [The big picture: one event's journey](#4-the-big-picture-one-events-journey)
5. [Architecture: the modules and why they exist](#5-architecture-the-modules-and-why-they-exist)
6. [Deep dive: talking to AutoElevate](#6-deep-dive-talking-to-autoelevate)
7. [Deep dive: talking to NinjaOne](#7-deep-dive-talking-to-ninjaone)
8. [Deep dive: never filing the same ticket twice](#8-deep-dive-never-filing-the-same-ticket-twice)
9. [Deep dive: the sync loop's failure semantics](#9-deep-dive-the-sync-loops-failure-semantics)
10. [Running it for real](#10-running-it-for-real)
11. [Testing, and the built-in test library](#11-testing-and-the-built-in-test-library)
12. [Extending the project](#12-extending-the-project)
13. [Glossary](#13-glossary)

---

## 1. What this project does, in one paragraph

Windows normally asks "do you want to allow this app to make changes?" and an
administrator has to say yes. **AutoElevate** (a CyberFOX product) manages those
moments across a whole fleet of computers, and it records each one as an
*elevation event*. **NinjaOne** is the system where IT technicians track their
work as *tickets*. This project is the bridge between the two: every few
minutes it asks AutoElevate "what elevation events happened since I last
checked?", and for each new one it creates a matching ticket in NinjaOne — so
technicians see privilege-elevation activity in the same queue as the rest of
their work, exactly once per event, even across crashes and restarts.

```
AutoElevate Partner API  ──poll──▶  normalize ──▶ map to org ──▶  POST /v2/ticketing/ticket
                                        │                              (NinjaOne)
                                        └── cursor + dedupe state (state.json)
```

---

## 2. The world this lives in

### MSPs

An **MSP** (Managed Service Provider) is an IT company that manages computers
for many client businesses at once. Everything in this project is shaped by
that: one AutoElevate account covers many *companies*, one NinjaOne account
covers many *organizations*, and part of this tool's job is matching a company
on one side to the right organization on the other.

### AutoElevate (the source)

Regular users shouldn't have administrator rights — that's how malware gets
installed. But sometimes a legitimate program genuinely needs admin rights.
AutoElevate is a **PAM** (Privileged Access Management) tool: an agent on each
computer intercepts these moments, can ask a technician for approval in real
time, and can auto-approve things that match rules ("installers signed by this
vendor are fine"). Every one of these moments becomes an **elevation event**:
which computer, which user, which program, what path and signer, which rule
applied, when.

AutoElevate exposes those events through its **Partner API** (currently in
beta) — a web service that other programs can query.

### NinjaOne (the destination)

NinjaOne is an **RMM** (Remote Monitoring and Management) platform — the
MSP's control panel for all managed devices — and it includes a **ticketing**
module. A *ticket* is a unit of work: a subject line, a description, a status,
a priority, and the *organization* (client company) it belongs to. Filing a
ticket per elevation event gives technicians visibility and an audit trail.

---

## 3. Crash course: the technology concepts

Skip this section if you already build API integrations.

### APIs, REST, and JSON

An **API** (Application Programming Interface) is how one program talks to
another. Web APIs work like a browser visiting pages: the client sends an
**HTTP request** to a **URL** and gets back a **response** with a numeric
**status code** (200 = OK, 401 = bad credentials, 404 = not found, 500 =
server broke) and a body. The style both services here use is called **REST**:
each *thing* has a URL (`/api/v1/elevation-events`), and the **HTTP method**
says what to do with it — `GET` reads, `POST` creates.

The bodies are **JSON**, a text format for structured data:

```json
{
  "computerName": "WS-FINANCE-04",
  "data": { "user": { "name": "jdoe" } },
  "createdAt": 1786874400000
}
```

That `createdAt` number is an **epoch timestamp**: milliseconds since midnight
UTC on January 1, 1970. It looks opaque but compares and sorts trivially.

### Authentication: proving who you are

- **API key / Bearer token** (AutoElevate): you're given a long secret string
  once; you send it on every request in a header —
  `Authorization: Bearer <key>`. Simple, but the key never changes, so guard
  it carefully.
- **OAuth 2.0 client credentials** (NinjaOne): you hold a `client_id` and
  `client_secret`, and you trade them for a short-lived **access token**
  (expires in ~1 hour). You send *that* on each request, and get a fresh one
  when it expires. More moving parts, but a stolen token dies quickly.

### Pagination

APIs won't return a million records in one response. You fetch a **page** at a
time. The two common dialects:

- **page-number**: `?page=3&limit=100` — "give me page 3".
- **offset**: `?skip=200&take=100` — "skip 200 records, give me 100".

AutoElevate uses the offset dialect. The difference matters: this codebase
originally guessed page-number, and sending `page=2` to an API that reads it
as `skip=2` silently *re-fetches almost everything* — a bug class worth
remembering.

### Polling vs. webhooks

Two ways to learn about new events: the server pushes them to you the moment
they happen (**webhooks**), or you ask on a schedule (**polling**). Webhooks
are instant but require you to run a public server. Polling is simpler and
survives downtime naturally — anything you missed is still there next time
you ask. This project polls, and the entire state-management design (section
8) follows from that choice.

---

## 4. The big picture: one event's journey

Follow a single event through one poll cycle:

```
 1. state.json        →  "cursor: 2026-08-16T10:04:00Z"  (how far we've read)
 2. GET /api/v1/elevation-events?start=1786874640000&take=100&skip=0
        Authorization: Bearer <AE key>
        X-Acknowledgment: i-understand-this-is-beta-and-may-change
 3. ← {"items": [ {...event...} ], "totalCount": 1}
 4. normalize      raw JSON → ElevationEvent(computer_name="WS-042", user_name="jdoe", …)
 5. dedupe check   have we ticketed this event id before? (skip if so)
 6. filter         does it pass SYNC_EVENT_TYPES / SYNC_SEVERITIES? (skip if not)
 7. map            company "Acme Corp" → NinjaOne organization 12
 8. build ticket   subject "[AutoElevate] setup.exe - jdoe on WS-042", body, tags
 9. POST /v2/ticketing/ticket   (with an OAuth token, fetched/refreshed as needed)
10. record         remember the event id, advance the cursor, save state.json
```

Every step lives in its own module, which is the next section.

---

## 5. Architecture: the modules and why they exist

```
src/cyberfox_into_ninja/
  config.py       reads environment variables into typed config objects
  http.py         one retry/backoff helper both clients share
  autoelevate.py  Partner API client (reads events)             ┐ the two
  ninjaone.py     NinjaOne client (OAuth + creates tickets)     ┘ "arms"
  models.py       ElevationEvent + tolerant field normalization
  mapper.py       event → ticket payload; company → organization
  state.py        cursor + dedupe history, saved atomically
  sync.py         the orchestrator that runs one poll cycle
  cli.py          command-line entry: check / run-once / poll / show-state
  testing.py      unit-test library: in-process fakes of both APIs
```

The design principle throughout: **each module owns one risk**.

- `config.py` owns the risk of *the beta API changing*. Every URL, parameter
  name, and header is an environment variable with a sensible default, so a
  surface change is a `.env` edit, not a code change.
- `http.py` owns the risk of *flaky networks*. Both clients funnel every
  request through one helper that retries transport errors and
  408/425/429/500/502/503/504 with exponential backoff and jitter, honouring
  `Retry-After`. Neither client needs its own retry logic.
- `models.py` owns the risk of *unknown field names*. Each normalized field
  (computer name, user, process, …) is found by probing a list of candidate
  keys (`computerName`, `computer_name`, `computer.name`, `data.user.name`,
  …). A newly discovered spelling is a one-line addition to
  `FIELD_CANDIDATES`.
- `state.py` owns the risk of *crashing mid-run* (section 8).
- `sync.py` owns the risk of *partial failure* (section 9).
- `testing.py` owns the risk of *tests that lie* — it fakes both APIs
  accurately enough that tests drive the real clients (section 11).

The dependency direction is strict: `sync.py` knows about everything below
it; the clients know nothing about sync, tickets, or each other. That is what
makes each piece testable alone.

---

## 6. Deep dive: talking to AutoElevate

`autoelevate.py` implements the confirmed Partner API surface
([partner-api-docs.autoelevate.com](https://partner-api-docs.autoelevate.com),
spec v1.0.0):

| Aspect | Value |
|---|---|
| Base URL | `https://partner-api.autoelevate.com` |
| Events | `GET /api/v1/elevation-events` (needs the `eventView` scope) |
| Auth | `Authorization: Bearer <key>` — key created with the **AE-BEARER** scheme |
| Paging | `take` (max 200) + `skip` |
| Time filter | `start` / `end`, epoch **milliseconds** |
| Envelope | `{"items": [...], "totalCount": n}` |

Three details deserve attention:

**The beta acknowledgment header.** Every request must carry, verbatim:

```
X-Acknowledgment: i-understand-this-is-beta-and-may-change
```

Without it the API refuses the request (HTTP 406). The client sends it
automatically; `AE_ACK_VALUE=` (empty) turns it off once the API leaves beta.

**Pagination stops three ways.** The paging loop ends when the envelope's
`totalCount` says we've read everything, *or* (if there's no `totalCount`) when
a page comes back shorter than requested, *or* when a page repeats the
previous page's ids. That last one is paranoia that pays for itself: if a
parameter name is ever wrong again, the failure mode is "fetched one page,"
not "infinite loop."

**Why `totalCount` matters.** The server caps `take` at 200. If you configure
`AE_PAGE_SIZE=500`, every page arrives "short" (200 < 500), and a naive
short-page check would stop after one page and silently drop events. Preferring
`totalCount` over the short-page heuristic closes that hole — a bug that was
actually found by this project's own fake server (section 11).

Also worth knowing: the API offers a second, stronger auth scheme
(`AE-HMAC-SHA256`, request signing with a never-transmitted key). This client
speaks Bearer only; HMAC is a possible future hardening step.

---

## 7. Deep dive: talking to NinjaOne

`ninjaone.py` handles two jobs:

**Getting a token.** `POST /ws/oauth/token` with
`grant_type=client_credentials`, the client id/secret, and the scope. The
response contains `access_token` and `expires_in`. The client caches the token
in memory and refreshes it 60 seconds *before* expiry (so a token never dies
mid-request), and if an API call still comes back 401 — a revoked token, say
— it refreshes once and retries.

**Creating the ticket.** `POST /v2/ticketing/ticket` with a JSON body built by
`mapper.py`:

```json
{
  "clientId": 12,
  "subject": "[AutoElevate] elevation_request setup.exe - jdoe on WS-042",
  "description": { "public": false, "body": "Event ID ... Path ... Publisher ..." },
  "status": "NEW", "type": "PROBLEM", "priority": "MEDIUM", "severity": "MODERATE",
  "tags": ["autoelevate", "elevation-request"]
}
```

`clientId` here is the NinjaOne **organization** id — which client company the
ticket belongs to. Finding it is `OrganizationResolver`'s job: it looks up the
event's AutoElevate company id, then company name (case-insensitively), in a
JSON map file you provide, falling back to `NINJA_DEFAULT_ORGANIZATION_ID`.
If neither produces an answer, the event is *not* ticketed and *not* marked
done — it waits for you to fix the map (see section 9).

One regional wrinkle: NinjaOne runs separate hosts per region (`us`, `us2`,
`eu`, `ca`, `oc`). `NINJA_REGION` picks the host; both OAuth and the API live
on it.

---

## 8. Deep dive: never filing the same ticket twice

This is the heart of the design. Two mechanisms work together, both persisted
in `state.json`:

**The cursor** is a timestamp: "I have handled everything up to here." Each
poll asks only for events after the cursor. It moves forward only — never
backward — so a weird event with no timestamp can't drag it into re-reading
history. On the very first run there is no cursor, so the tool reaches back
`SYNC_INITIAL_LOOKBACK_MINUTES` (default 60).

**The processed-id set** is the second line of defense. Time filters are
inherently fuzzy at the boundary (two events in the same millisecond; a fetch
that includes the cursor moment itself), and crashes can leave the cursor
slightly behind reality. So the state also remembers the ids of the last 5,000
events already handled, and anything already in that set is skipped no matter
what the cursor says. It's a bounded FIFO — old ids fall off the back — so the
file can't grow forever. Events that arrive with no id at all get a synthetic
one: a SHA-256 hash of their full content, which is stable for the same event
re-fetched later.

**Atomic saves.** `state.json` is written to a temp file first, then swapped
into place with an atomic rename (`os.replace`). A crash mid-write leaves the
*old* state intact, never a half-written file. And note the failure direction:
state is saved *after* tickets are created, so a crash at the worst moment
re-processes an event — and the processed-id set catches it. The design errs
toward "never lose an event" and uses dedupe to also achieve "never duplicate
one."

---

## 9. Deep dive: the sync loop's failure semantics

`sync.py` runs the cycle from section 4, and its interesting decisions are all
about *partial failure*:

**Oldest first.** Fetched events are sorted by timestamp before processing, so
the cursor can advance incrementally: if event 7 of 10 fails, events 1–6 are
already committed and only 7–10 remain for next cycle.

**The cursor blocks on failure.** When a ticket fails to create, or an event's
company can't be mapped to an organization, the loop sets a `cursor_blocked`
flag: later events in the same run are still attempted (one bad client
shouldn't stall nine good ones), but the cursor stops advancing at the
failure. Next cycle re-fetches from that point; the already-ticketed later
events are absorbed by the dedupe set. The failed event gets retried
automatically — including the unmapped one, once you've added its company to
the org map. Nothing is ever silently dropped.

**Counters, not just logs.** Each run returns
`fetched / created / duplicates / filtered / unmapped / failed`, and
`needs_attention` is true whenever `failed` or `unmapped` is nonzero — a ready-
made hook for monitoring.

**Dry run.** `SYNC_DRY_RUN=true` (or `--dry-run`) walks the entire pipeline,
logs the tickets it *would* create, and — crucially — does not persist state,
so a later real run processes the same events for real.

---

## 10. Running it for real

### Setup

```bash
git clone https://github.com/LanServicesFbgLLC/cyberfox-into-ninja.git
cd cyberfox-into-ninja
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # then fill it in
```

Credentials you need:

- **AutoElevate**: in the admin portal, create an API key — **AE-BEARER**
  scheme, `eventView` permission, ideally on a *Service* user so the audit
  trail is clean. The secret is shown once; store it as `AE_API_KEY`.
- **NinjaOne**: Administration → Apps → API → create a client with grant type
  *Client Credentials*; store id and secret as `NINJA_CLIENT_ID` /
  `NINJA_CLIENT_SECRET`, and set `NINJA_REGION`.
- **Mapping**: copy `organizations.example.json`, map each AutoElevate company
  name (or id) to its NinjaOne organization id, point `SYNC_ORG_MAP_PATH` at
  it — and/or set `NINJA_DEFAULT_ORGANIZATION_ID` as a catch-all.

### Commands

```bash
cyberfox-into-ninja check      # verify both APIs answer with your credentials
cyberfox-into-ninja run-once --dry-run   # full pipeline, no tickets created
cyberfox-into-ninja run-once   # one real cycle
cyberfox-into-ninja poll       # loop forever (SYNC_POLL_INTERVAL_SECONDS, default 300)
cyberfox-into-ninja show-state # inspect the cursor + dedupe history
```

### Deployment patterns

`run-once` from cron / Windows Task Scheduler is the simplest reliable setup —
the process is short-lived and state lives on disk. `poll` under a service
manager (systemd, NSSM) works equally well. Either way, watch the logs for
`unmapped` warnings: each one is a client company waiting to be added to the
org map, and its events wait with it.

### Recovery cheat-sheet

- **Replay a window**: delete (or edit the cursor in) `state.json` — the
  dedupe set still prevents duplicate tickets for anything already filed.
- **Full reset**: delete `state.json` entirely; the next run starts from the
  initial lookback.
- **One event ticketed twice anyway?** It exceeded the 5,000-id dedupe
  history *and* predated the cursor — raise `SYNC_DEDUPE_HISTORY`.

---

## 11. Testing, and the built-in test library

```bash
.venv/bin/python -m pytest -q
```

The suite runs with **no network and no credentials** — every HTTP interaction
is faked in-process. Beyond the tests themselves, the project ships its fakes
as a reusable library, `cyberfox_into_ninja.testing`:

```python
from cyberfox_into_ninja.testing import FakeNinjaOne, FakePartnerAPI

api = FakePartnerAPI()
api.add_event(computerName="WS-042")
with api.autoelevate_client() as client:   # a REAL AutoElevateClient
    events = client.fetch_events()

ninja = FakeNinjaOne()
with ninja.ninjaone_client() as client:    # a REAL NinjaOneClient
    client.create_ticket({"subject": events[0].describe()})
assert ninja.tickets[0]["id"] == 1
```

The point of the fakes is that they *enforce what the real services enforce*:
`FakePartnerAPI` rejects bad keys, 406s a missing acknowledgment header, caps
`take` at 200, and pages with real offset arithmetic; `FakeNinjaOne` issues
and validates OAuth tokens (with `expire_tokens()` to exercise the
refresh-and-retry path) and captures every ticket. Tests therefore drive the
production client code down its real paths instead of asserting against
hand-written mock responses that might drift from reality.

That fidelity has already earned its keep once: the `take`-cap behaviour in
the fake exposed the real pagination bug described in section 6 before it
could drop events in production.

`tests/test_testing_lib.py` is a working example of every feature.

---

## 12. Extending the project

Ideas in rough order of value, with where they'd land:

- **Approve/deny from tickets.** The Partner API also exposes
  `GET /api/v1/elevation-requests` and
  `POST /api/v1/elevation-requests/{id}/approve` / `.../deny`. A future
  version could sync *pending requests* and act on technician responses —
  turning this from a reporting bridge into a workflow bridge.
  (`autoelevate.py` + a new sync mode.)
- **A newly discovered event field** → one line in
  `models.py::FIELD_CANDIDATES`, then surface it in `mapper.py::build_body`.
- **Different ticket routing** (per-severity priorities, per-company forms) →
  `mapper.py::build_ticket`, driven by new config in `config.py`.
- **HMAC auth** (`AE-HMAC-SHA256` request signing) → `autoelevate.py`
  `_auth_headers`, computing the signature described in the spec's security
  scheme.
- **A different destination** (another PSA instead of NinjaOne) → implement a
  client with a `create_ticket(dict)` method and a mapper; `sync.py` doesn't
  care what's behind those.
- **Metrics/alerting** → `SyncResult` already carries the counters; export
  them from `cli.py`.

When the beta API changes shape, remember the standing rule: reach for
`.env` (`AE_*` overrides) first, code second.

---

## 13. Glossary

| Term | Meaning |
|---|---|
| **API** | An interface one program exposes so other programs can use it |
| **Bearer token** | A secret string sent in the `Authorization` header to prove identity |
| **Cursor** | A saved "read up to here" marker that makes polling incremental |
| **Dedupe** | Detecting and skipping something already processed |
| **Elevation** | Temporarily granting administrator rights for one action |
| **Epoch milliseconds** | A timestamp counted as ms since 1970-01-01 UTC |
| **Idempotent** | Safe to repeat — doing it twice has the same effect as once |
| **JSON** | The text format both APIs use for structured data |
| **MSP** | Managed Service Provider — an IT company serving many client businesses |
| **OAuth client credentials** | A flow that trades an id+secret for a short-lived access token |
| **Organization** | NinjaOne's term for a client company (AutoElevate says *company*) |
| **PAM** | Privileged Access Management (AutoElevate's category) |
| **Pagination** | Fetching a large result set one page at a time (`take`/`skip` here) |
| **Polling** | Asking for new data on a schedule (vs. webhooks, where data is pushed) |
| **REST** | API style: URLs name things, HTTP methods act on them |
| **RMM** | Remote Monitoring and Management (NinjaOne's category) |
| **Ticket** | A tracked unit of work in NinjaOne's ticketing module |
