# cyberfox-into-ninja

Polls the **CyberFOX AutoElevate Partner API (beta)** for elevation events and
files a matching **NinjaOne** ticket for each one.

```
AutoElevate Partner API  ──poll──▶  normalize ──▶ map to org ──▶  POST /v2/ticketing/ticket
                                        │                              (NinjaOne, us2)
                                        └── cursor + dedupe state (state.json)
```

Run it as a one-shot from cron/Task Scheduler, or leave `poll` running as a service.

---

## ⚠️ Read this before you run it

The AutoElevate Partner API is in beta, and **its documentation was not
reachable from the environment this was built in** —
`partner-api-docs.autoelevate.com` and `support.cyberfox.com` are both blocked
by the network egress policy. So the following are *configurable assumptions*,
not confirmed facts:

| Assumption | Env var | Default |
|---|---|---|
| API base URL | `AE_BASE_URL` | `https://api.autoelevate.com` |
| Events endpoint | `AE_EVENTS_PATH` | `/v1/events` |
| Auth scheme | `AE_AUTH_STYLE` | `bearer` |
| "Newer than" filter param | `AE_SINCE_PARAM` | `since` |
| Paging params | `AE_PAGE_PARAM`, `AE_PAGE_SIZE_PARAM` | `page`, `limit` |

Reconcile these against the current beta docs, then set them in `.env`. **No
code change should be needed** — that is the whole reason they are config.

Two things are deliberately tolerant, so a wrong guess degrades gracefully
rather than breaking or looping:

- **Response envelope** — the events array is found whether the API returns a
  bare list or wraps it in `data` / `items` / `results` / `events` / `records` /
  `content` (including one level of nesting).
- **Field names** — each normalized field probes a list of candidate keys
  (`computerName`, `computer_name`, `computer.name`, `hostname`, …). Adding a
  newly-discovered key is a one-line change in
  `src/cyberfox_into_ninja/models.py` → `FIELD_CANDIDATES`.

On the NinjaOne side, ticket field names follow the documented
`POST /v2/ticketing/ticket` schema, but `ticketFormId` and the allowed
`status` / `priority` / `severity` / `type` values are tenant-specific —
verify them against your instance. Ticketing must be enabled on the tenant;
if it is not, you will get a 404 with an explanatory message.

**Nothing here has been run against the live APIs** — both hosts are blocked in
the build environment. The test suite covers behaviour against mocked HTTP.
Start with `check`, then `--dry-run`.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
$EDITOR .env
```

### NinjaOne credentials

Administration → Apps → API → **Add**, with:

- Application platform: **API services (machine-to-machine)**
- Grant type: **Client Credentials**
- Scopes: `monitoring` and `management` (management is what ticketing needs)

Put the client id/secret into `NINJA_CLIENT_ID` / `NINJA_CLIENT_SECRET`.
Region is preset to `us2` (`us2.ninjarmm.com`); change `NINJA_REGION` or set
`NINJA_HOST` outright if that moves.

### Where tickets land

Every ticket needs a NinjaOne organization. Set **at least one** of:

- `NINJA_DEFAULT_ORGANIZATION_ID` — a single org that catches everything, or
- `SYNC_ORG_MAP_PATH` — a JSON file mapping AutoElevate company id **or** name
  (case-insensitive) to a NinjaOne org id. See `organizations.example.json`.

Set both and the map wins, with the default as fallback. Set neither and
`check` fails loudly rather than letting a poll silently drop everything.

---

## Usage

```bash
cyberfox-into-ninja check        # validate config + both APIs, exit 1 on failure
cyberfox-into-ninja run-once     # one cycle, then exit  (good for cron)
cyberfox-into-ninja poll         # loop on SYNC_POLL_INTERVAL_SECONDS
cyberfox-into-ninja show-state   # print the cursor and dedupe history
```

Global flags: `--dry-run` (map and log tickets, create nothing, persist
nothing), `-v` (debug logging), `--env-file PATH`.

Suggested first run:

```bash
cyberfox-into-ninja check
cyberfox-into-ninja --dry-run -v run-once   # inspect the tickets it *would* file
cyberfox-into-ninja run-once
```

Cron, every 5 minutes:

```cron
*/5 * * * * cd /opt/cyberfox-into-ninja && .venv/bin/cyberfox-into-ninja run-once >> sync.log 2>&1
```

---

## How duplicates are prevented

`state.json` holds two things:

- **`cursor`** — the newest event timestamp fully handled. The next fetch asks
  for events after it. The cursor only ever moves forward.
- **`processed_ids`** — a bounded FIFO (`SYNC_DEDUPE_HISTORY`, default 5000) of
  event ids already ticketed, which catches overlap at the cursor boundary and
  reruns of the same window.

Events with no id of their own are identified by a SHA-256 of their payload, so
they dedupe too.

Failures are handled so nothing is silently lost: if an event can't be mapped to
an organization, or NinjaOne rejects the ticket, **the cursor stops advancing at
that event** while later events in the batch still get processed. The stuck
event is retried on the next cycle, once you have fixed the org map or whatever
NinjaOne objected to. `run-once` exits non-zero when tickets failed.

State is written atomically (temp file + `os.replace`), so an interrupted run
cannot corrupt the cursor.

---

## Filtering

Both are comma-separated and case-insensitive; empty means "everything":

```bash
SYNC_EVENT_TYPES=elevation_request,denied
SYNC_SEVERITIES=high,critical
```

Filtered events are marked handled and advance the cursor — they are a
deliberate skip, not a pending item.

---

## Layout

```
src/cyberfox_into_ninja/
  config.py       env-driven config, NinjaOne region map, minimal dotenv loader
  models.py       ElevationEvent + tolerant field/timestamp normalization
  autoelevate.py  Partner API client (beta surface, config-driven)
  ninjaone.py     OAuth2 client_credentials + ticket creation
  mapper.py       event -> ticket payload, company -> org resolution
  state.py        cursor + dedupe history, atomic persistence
  sync.py         the poll cycle and its counters
  cli.py          check / run-once / poll / show-state
  http.py         retry + backoff, Retry-After aware
```

Retries cover transport errors and 408/425/429/500/502/503/504 with exponential
backoff and jitter, honouring `Retry-After`. NinjaOne 401s trigger one token
refresh and retry.

Only runtime dependency is `httpx`.

## Tests

```bash
python -m pytest -q
```

87 tests, all HTTP mocked with `respx` — no network, no credentials needed.
