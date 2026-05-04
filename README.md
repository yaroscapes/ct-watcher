# watch

Polls a third-party Waitwhile booking flow on a schedule and pushes a
notification when a previously-unavailable target gains a real bookable
slot inside a configurable date window.

Pure Python stdlib (`urllib`, `json`, `zoneinfo`); no third-party
dependencies, no browser.

## Why

[Waitwhile](https://waitwhile.com) is used by various businesses for
appointment booking. The booking page itself calls a public JSON API:

```
GET https://api.waitwhile.com/v2/public/visits/<locationId>/first-available-slots
    ?fromDate=…&toDate=…&maxNumSlots=…&resourceIds=…&serviceIds=…
```

This requires no authentication once you have the right query
parameters. An empty `[]` response means nothing is bookable; a
non-empty array means there's a slot the user could click to book.

## Configuration

All identifying information lives **outside the repo**. The watcher
reads its config from the first source that resolves:

1. `CONFIG_JSON` environment variable — raw JSON. Used in CI.
2. `CONFIG_FILE` environment variable — path to a JSON file.
3. `.config.local.json` next to `watcher.py` — gitignored, used for
   local development.
4. `config.example.json` — placeholder. Refuses to run with
   `__REPLACE_ME__` values.

See [`config.example.json`](config.example.json) for the schema.

The `service.service_ids` and `service.resource_ids` for any given
Waitwhile merchant can be discovered by visiting the booking page in a
real browser and inspecting `GET https://api.waitwhile.com/v2/public/services/<locationId>`
(returns the service catalogue under the "Services" category's
`children` array).

## Run locally

```bash
# Drop your real config into .config.local.json (gitignored).
cp config.example.json .config.local.json
# … edit it …

./run.sh                         # one-shot
./run.sh --loop                  # poll every N seconds (config-controlled)
./run.sh --loop --interval 300   # override interval
```

For phone push, install [ntfy](https://ntfy.sh) on your phone, pick a
hard-to-guess topic name, and:

```bash
export NTFY_TOPIC=<your-private-topic>
./run.sh --loop
```

macOS native notifications (via `osascript`) fire automatically when
`notifications.macos: true` in the config.

## Run on GitHub Actions

[`.github/workflows/watch.yml`](.github/workflows/watch.yml) runs every
15 minutes via cron and on manual `workflow_dispatch`. Two repository
secrets are required:

- **`CONFIG_JSON`** — the contents of your real config as a single
  JSON string. Set under **Settings → Secrets and variables →
  Actions → New repository secret**.
- **`NTFY_TOPIC`** — your ntfy.sh topic name (push goes to your
  phone).

### A note on logging in public repos

This workflow's runs are visible to anyone in a public repository. The
watcher's log output is **deliberately generic**:

```
[2026-05-04 08:55:30] checking 5 target(s), window=14d
  Target 1: no slots
  Target 2: no slots
  …
  -> notified target 3 (2 new date(s))
```

Target names, dates, IDs, response bodies, and stack traces are never
printed. Notification content (which does contain target name and
dates) is sent over HTTPS to ntfy.sh as request body and headers, never
to the workflow log.

## Files

| File | What it does |
|---|---|
| `watcher.py` | Entry point; loads config, drives passes, manages state |
| `checker.py` | One Waitwhile API call per target, parses bookable dates |
| `notify.py` | macOS native + ntfy.sh push |
| `config.example.json` | Schema reference (placeholder values) |
| `.config.local.json` | Real config for local dev (gitignored) |
| `.state.json` | Per-target last-seen open dates (gitignored) |
| `.github/workflows/watch.yml` | Cron schedule (every 15 min) |
