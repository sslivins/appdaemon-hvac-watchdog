# appdaemon-hvac-watchdog

An [AppDaemon](https://appdaemon.readthedocs.io/) app for Home Assistant that
**watches a set of Mitsubishi mini-split `climate.*` entities** (driven by ESP32
boards running the [gysmo38/mitsubishi2MQTT](https://github.com/gysmo38/mitsubishi2MQTT)
firmware) and automatically recovers them when the firmware locks up and the unit
drops to `unavailable` in Home Assistant.

> Personal project, shared as-is. Adapt the entity IDs / hosts in
> `hvac_watchdog.yaml` to your own setup.

## What it does

1. **Tracks availability.** For each watched unit it counts how often it drops to
   `unavailable`, how long it stays down (total downtime), and how many times it
   has been rebooted. Stats persist to `state.json` so they survive AppDaemon
   reloads, and are optionally republished as `sensor.*` entities you can graph.
2. **Auto-recovers via REST.** When a unit stays `unavailable` past a grace
   period, the app logs in to the board's web UI and issues `GET /reboot` to
   power-cycle the ESP32. A cooldown and a cap on consecutive reboots prevent
   reboot loops; once the cap is hit it stops and (optionally) fires a
   notification instead.

### Recovery flow

```
unavailable ---(stays down unavailable_grace_seconds)---> reboot
   ^                                                         |
   |                                                         v
 recover <----(re-check after reboot_cooldown_seconds)--- still down?
   |                                                         |
   | reset escalation                          up to max_consecutive_reboots,
   |                                              then notify + give up
```

A clean recovery (the unit returning to a normal state) resets the escalation,
so the next outage starts fresh.

## Authentication

The mitsubishi2MQTT web UI uses cookie auth with a **hard-coded username
`admin`** and a static session cookie `M2MSESSIONID=1` set on a successful
`POST /login`. The app POSTs the login form (`USERNAME` / `PASSWORD`) and also
sets the cookie explicitly, then calls `GET /reboot`. All four units typically
share the same web password.

## Installation

1. Copy this folder into your AppDaemon `apps/` directory
   (e.g. `apps/hvac_watchdog/`).
2. `cp secrets.yaml.example secrets.yaml` and put your real device web password
   in it (gitignored, never committed).
3. Edit `hvac_watchdog.yaml` for your own entities, hosts, and policy.

AppDaemon auto-discovers `hvac_watchdog.yaml` and loads `hvac_watchdog.py`. The
only Python dependency is `requests`, which ships with the standard AppDaemon
image.

## Logging (optional)

The app config sets `log: hvac_watchdog_log`. To route its output to a dedicated
rotating file, add a matching logger to the `logs:` section of your
**`appdaemon.yaml`**:

```yaml
logs:
  hvac_watchdog_log:
    name: HvacWatchdog
    filename: /conf/apps/hvac_watchdog/hvac_watchdog.log
    log_size: 1048576       # 1 MB per file
    log_generations: 10     # keep 10 rotated files
    format: "{asctime}.{msecs:03.0f} {levelname:<7} {message}"
    date_format: "%Y-%m-%d %H:%M:%S"
```

Changes to `appdaemon.yaml` require an AppDaemon restart. To skip the dedicated
logger, remove the `log:` line from `hvac_watchdog.yaml`.

## Configuration (`hvac_watchdog.yaml`)

| Key | What it is |
| --- | --- |
| `username` | Web UI username (firmware hard-codes `admin`). |
| `password` | Web UI password, via `!secret` from `secrets.yaml`. |
| `unavailable_grace_seconds` | How long a unit must stay down before rebooting (default 300). |
| `reboot_cooldown_seconds` | Wait after a reboot before re-checking/rebooting again (default 300). |
| `max_consecutive_reboots` | Stop + notify after this many reboots without recovery (default 3). |
| `http_timeout_seconds` | Per-request HTTP timeout to a board (default 10). |
| `reboot_enabled` | `false` = only track availability, never reboot (default `true`). |
| `publish_sensors` | Publish `sensor.*` stats (default `true`). |
| `sensor_prefix` | Prefix for the published sensors (default `hvac_watchdog`). |
| `notify_service` | Optional `notify.*` service called when a unit can't be recovered. |
| `unavailable_states` | States that count as "down" (default `unavailable`, `unknown`). |
| `devices[]` | `entity` + `host` (IP); optional `tracker` (device_tracker for a live `ip`) and `name`. |

### Published sensors

For each device (`name`), with the default prefix:

- `sensor.hvac_watchdog_<name>_status` — `available` / `unavailable`
- `sensor.hvac_watchdog_<name>_unavailable_count` — lifetime drop count
- `sensor.hvac_watchdog_<name>_reboots` — lifetime reboot count

Each carries attributes: `host`, `source_entity`, `total_unavailable_seconds`,
`last_unavailable`, `last_available`, `last_reboot`, `consecutive_reboots`,
`gave_up`.

## Manual reboot (testing)

Fire the event `hvac_watchdog_reboot` to reboot on demand without waiting for an
outage:

```yaml
# Developer Tools -> Events  (or an automation / AppDaemon)
event_type: hvac_watchdog_reboot
event_data:
  entity: climate.master_bedroom   # or "all"
```

## Files

| File | Purpose |
| --- | --- |
| `hvac_watchdog.py` | The app logic. |
| `hvac_watchdog.yaml` | App configuration (edit for your setup). |
| `secrets.yaml.example` | Template for the gitignored `secrets.yaml`. |

## License

MIT
