"""HVAC Watchdog - AppDaemon app.

Monitors a set of Mitsubishi mini-split ``climate.*`` entities (driven by ESP32
boards running the gysmo38/mitsubishi2MQTT firmware) and:

1. **Tracks availability** - counts how often each unit drops to ``unavailable``
   in Home Assistant, how long it stays down, and how many times it has been
   rebooted. Stats persist to ``state.json`` (survives AppDaemon reloads) and are
   optionally republished as ``sensor.*`` entities so they can be graphed.
2. **Auto-recovers via REST** - when a unit stays ``unavailable`` past a grace
   period (suspected firmware lock-up), the app logs in to the board's web UI and
   hits ``GET /reboot`` to power-cycle the ESP32. A cooldown plus a cap on
   consecutive reboots prevents reboot loops; once the cap is hit the app stops
   and (optionally) fires a notification instead.

The mitsubishi2MQTT web UI uses cookie auth with a hard-coded username ``admin``
and a static session cookie ``M2MSESSIONID=1`` set on a successful
``POST /login``. We both POST the login form and set the cookie explicitly, then
``GET /reboot``.

No entity IDs, hosts, or secrets are hard-coded - everything comes from the
app's YAML config (with the password supplied via ``!secret``).
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass

import requests


# Stats fields we persist per device (runtime-only fields live in self._runtime).
_PERSIST_FIELDS = (
    "unavailable_count",
    "total_unavailable_seconds",
    "unavailable_since",   # epoch float or None - lets downtime survive reloads
    "last_unavailable_ts",
    "last_available_ts",
    "reboot_count",
    "last_reboot_ts",
)


class HvacWatchdog(hass.Hass):

    def initialize(self):
        # --- Config -----------------------------------------------------
        self.username = str(self.args.get("username", "admin"))
        self.password = str(self.args.get("password", ""))
        self.grace_seconds = int(self.args.get("unavailable_grace_seconds", 300))
        self.cooldown_seconds = int(self.args.get("reboot_cooldown_seconds", 300))
        self.max_reboots = int(self.args.get("max_consecutive_reboots", 3))
        self.http_timeout = int(self.args.get("http_timeout_seconds", 10))
        self.reboot_enabled = bool(self.args.get("reboot_enabled", True))
        self.publish_sensors = bool(self.args.get("publish_sensors", True))
        self.sensor_prefix = str(self.args.get("sensor_prefix", "hvac_watchdog"))
        self.notify_service = self.args.get("notify_service")  # e.g. notify.mobile_app_x
        # States that count as "the unit is down".
        self.down_states = {
            str(s).lower()
            for s in self._as_list(self.args.get("unavailable_states",
                                                 ["unavailable", "unknown"]))
        }

        # --- Devices ----------------------------------------------------
        # Each: {entity, host, [tracker], [name]}. `host` is the static IP;
        # optional `tracker` (a device_tracker) is consulted for a live `ip`
        # attribute first (DHCP resilience), falling back to `host`.
        self.devices = {}
        for d in self._as_list(self.args.get("devices", [])):
            entity = d["entity"]
            self.devices[entity] = {
                "entity": entity,
                "host": d.get("host"),
                "tracker": d.get("tracker"),
                "name": d.get("name") or entity.split(".")[-1],
            }

        # --- Persistence ------------------------------------------------
        app_dir = os.path.dirname(os.path.abspath(__file__))
        self.state_file = self.args.get(
            "state_file", os.path.join(app_dir, "state.json"))
        self._lock = threading.RLock()
        self.stats = self._load_stats()

        # --- Runtime (not persisted) ------------------------------------
        # Per entity: grace_handle, cooldown_until (epoch), consecutive_reboots,
        # gave_up (bool).
        self._runtime = {
            e: {"grace_handle": None, "cooldown_until": 0.0,
                "consecutive_reboots": 0, "gave_up": False}
            for e in self.devices
        }

        # --- Wiring -----------------------------------------------------
        for entity in self.devices:
            self.listen_state(self._on_state, entity)

        # Manual reboot trigger for testing: fire event "hvac_watchdog_reboot"
        # with data {"entity": "climate.master_bedroom"} (or "all").
        self.listen_event(self._on_manual_reboot, "hvac_watchdog_reboot")

        # Baseline current state without counting a phantom transition. If a
        # device is already down at startup, begin a grace countdown.
        now = time.time()
        for entity, dev in self.devices.items():
            cur = self.get_state(entity)
            if self._is_down(cur):
                st = self.stats[entity]
                if not st.get("unavailable_since"):
                    st["unavailable_since"] = now
                    st["last_unavailable_ts"] = self._iso(now)
                self._arm_grace(entity, "startup")
            self._publish(entity)
        self._save_stats()

        self.log(
            "Initialized. devices=%d grace=%ds cooldown=%ds max_reboots=%d "
            "reboot_enabled=%s down_states=%s"
            % (len(self.devices), self.grace_seconds, self.cooldown_seconds,
               self.max_reboots, self.reboot_enabled, sorted(self.down_states))
        )

    # ------------------------------------------------------------------ #
    # State handling
    # ------------------------------------------------------------------ #
    def _on_state(self, entity, attribute, old, new, kwargs):
        down_old = self._is_down(old)
        down_new = self._is_down(new)
        if down_new and not down_old:
            self._on_went_down(entity)
        elif down_old and not down_new:
            self._on_came_back(entity, new)

    def _on_went_down(self, entity):
        now = time.time()
        with self._lock:
            st = self.stats[entity]
            st["unavailable_count"] = st.get("unavailable_count", 0) + 1
            st["unavailable_since"] = now
            st["last_unavailable_ts"] = self._iso(now)
            self._save_stats_locked()
        self.log("%s went UNAVAILABLE (count=%d). Reboot in %ds if it stays down."
                 % (entity, self.stats[entity]["unavailable_count"],
                    self.grace_seconds))
        self._arm_grace(entity, "went_down")
        self._publish(entity)

    def _on_came_back(self, entity, new):
        now = time.time()
        with self._lock:
            st = self.stats[entity]
            since = st.get("unavailable_since")
            if since:
                st["total_unavailable_seconds"] = (
                    st.get("total_unavailable_seconds", 0.0) + (now - since))
            st["unavailable_since"] = None
            st["last_available_ts"] = self._iso(now)
            self._save_stats_locked()
        # A clean recovery resets the reboot escalation.
        rt = self._runtime[entity]
        rt["consecutive_reboots"] = 0
        rt["gave_up"] = False
        rt["cooldown_until"] = 0.0
        self._cancel_grace(entity)
        self.log("%s back ONLINE (state=%s). Reset reboot escalation." %
                 (entity, new))
        self._publish(entity)

    # ------------------------------------------------------------------ #
    # Reboot escalation
    # ------------------------------------------------------------------ #
    def _arm_grace(self, entity, reason):
        """(Re)schedule the grace check for a device, replacing any pending one."""
        self._cancel_grace(entity)
        self._runtime[entity]["grace_handle"] = self.run_in(
            self._grace_expired, self.grace_seconds, entity=entity, reason=reason)

    def _cancel_grace(self, entity):
        handle = self._runtime[entity].get("grace_handle")
        if handle is not None:
            try:
                self.cancel_timer(handle)
            except Exception:  # noqa: BLE001
                pass
            self._runtime[entity]["grace_handle"] = None

    def _grace_expired(self, kwargs):
        entity = kwargs["entity"]
        self._runtime[entity]["grace_handle"] = None

        # Recovered while we waited - nothing to do (handler already cleaned up).
        if not self._is_down(self.get_state(entity)):
            return

        rt = self._runtime[entity]
        if rt["gave_up"]:
            return

        now = time.time()
        if now < rt["cooldown_until"]:
            # Still settling after a reboot; re-check when the cooldown ends.
            remaining = max(1, int(rt["cooldown_until"] - now))
            self._runtime[entity]["grace_handle"] = self.run_in(
                self._grace_expired, remaining, entity=entity, reason="cooldown")
            return

        if not self.reboot_enabled:
            self.log("%s still down but reboot_enabled=false - not rebooting."
                     % entity, level="WARNING")
            self._arm_grace(entity, "disabled-recheck")
            return

        if rt["consecutive_reboots"] >= self.max_reboots:
            rt["gave_up"] = True
            msg = ("%s still UNAVAILABLE after %d reboot attempts - giving up "
                   "(manual intervention needed)."
                   % (entity, rt["consecutive_reboots"]))
            self.log(msg, level="ERROR")
            self._notify(msg)
            self._publish(entity)
            return

        self._attempt_reboot(entity)

    def _attempt_reboot(self, entity):
        dev = self.devices[entity]
        host = self._resolve_host(entity)
        rt = self._runtime[entity]
        if not host:
            self.log("%s: no host/IP resolved - cannot reboot." % entity,
                     level="WARNING")
            rt["consecutive_reboots"] += 1  # count as an attempt to honour the cap
            self._schedule_post_reboot(entity)
            return

        ok, detail = self._reboot_http(host)
        rt["consecutive_reboots"] += 1
        now = time.time()
        rt["cooldown_until"] = now + self.cooldown_seconds
        if ok:
            with self._lock:
                st = self.stats[entity]
                st["reboot_count"] = st.get("reboot_count", 0) + 1
                st["last_reboot_ts"] = self._iso(now)
                self._save_stats_locked()
            self.log("%s: reboot #%d sent to %s (%s). Re-checking in %ds."
                     % (entity, rt["consecutive_reboots"], host, detail,
                        self.cooldown_seconds))
        else:
            self.log("%s: reboot attempt #%d to %s FAILED (%s). Re-checking in %ds."
                     % (entity, rt["consecutive_reboots"], host, detail,
                        self.cooldown_seconds), level="WARNING")
        self._schedule_post_reboot(entity)
        self._publish(entity)

    def _schedule_post_reboot(self, entity):
        self._cancel_grace(entity)
        self._runtime[entity]["grace_handle"] = self.run_in(
            self._grace_expired, self.cooldown_seconds, entity=entity,
            reason="post-reboot")

    def _reboot_http(self, host):
        """POST /login then GET /reboot. Returns (ok, detail)."""
        base = "http://%s" % host
        try:
            s = requests.Session()
            # The firmware compares USERNAME to the literal "admin" and PASSWORD
            # to its configured web password, then sets cookie M2MSESSIONID=1.
            try:
                s.post(base + "/login",
                       data={"USERNAME": self.username, "PASSWORD": self.password},
                       timeout=self.http_timeout, allow_redirects=False)
            except requests.RequestException:
                pass  # fall through to the static-cookie shortcut
            # is_authenticated() only checks for this exact cookie value.
            s.cookies.set("M2MSESSIONID", "1")
            r = s.get(base + "/reboot", timeout=self.http_timeout,
                      allow_redirects=False)
            # The board reboots ~500ms after responding; a 2xx/3xx means accepted.
            if r.status_code < 400:
                return True, "HTTP %d" % r.status_code
            return False, "HTTP %d" % r.status_code
        except requests.RequestException as exc:
            return False, type(exc).__name__

    def _resolve_host(self, entity):
        dev = self.devices[entity]
        tracker = dev.get("tracker")
        if tracker:
            ip = self.get_state(tracker, attribute="ip")
            if ip:
                return str(ip)
        return dev.get("host")

    # ------------------------------------------------------------------ #
    # Manual trigger / notifications
    # ------------------------------------------------------------------ #
    def _on_manual_reboot(self, event_name, data, kwargs):
        target = data.get("entity", "all")
        targets = list(self.devices) if target == "all" else [target]
        for entity in targets:
            if entity not in self.devices:
                self.log("Manual reboot: unknown entity %s" % entity,
                         level="WARNING")
                continue
            self.log("Manual reboot requested for %s." % entity)
            self._attempt_reboot(entity)

    def _notify(self, message):
        if not self.notify_service:
            return
        try:
            domain, service = self.notify_service.split(".", 1)
            self.call_service("%s/%s" % (domain, service), message=message,
                              title="HVAC Watchdog")
        except Exception as exc:  # noqa: BLE001
            self.log("Notify failed: %s" % exc, level="WARNING")

    # ------------------------------------------------------------------ #
    # HA sensors
    # ------------------------------------------------------------------ #
    def _publish(self, entity):
        if not self.publish_sensors:
            return
        dev = self.devices[entity]
        st = self.stats[entity]
        rt = self._runtime[entity]
        down = self._is_down(self.get_state(entity))
        base = "sensor.%s_%s" % (self.sensor_prefix, dev["name"])
        common = {
            "host": self._resolve_host(entity) or "",
            "source_entity": entity,
            "total_unavailable_seconds": round(
                st.get("total_unavailable_seconds", 0.0), 1),
            "last_unavailable": st.get("last_unavailable_ts"),
            "last_available": st.get("last_available_ts"),
            "last_reboot": st.get("last_reboot_ts"),
            "consecutive_reboots": rt["consecutive_reboots"],
            "gave_up": rt["gave_up"],
        }
        try:
            self.set_state(base + "_status",
                           state="unavailable" if down else "available",
                           attributes=dict(common,
                                           friendly_name="%s HVAC status"
                                           % dev["name"]))
            self.set_state(base + "_unavailable_count",
                           state=st.get("unavailable_count", 0),
                           attributes=dict(common, unit_of_measurement="drops",
                                           friendly_name="%s HVAC drops"
                                           % dev["name"]))
            self.set_state(base + "_reboots",
                           state=st.get("reboot_count", 0),
                           attributes=dict(common, unit_of_measurement="reboots",
                                           friendly_name="%s HVAC reboots"
                                           % dev["name"]))
        except Exception as exc:  # noqa: BLE001
            self.log("Sensor publish failed for %s: %s" % (entity, exc),
                     level="WARNING")

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load_stats(self):
        data = {}
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as fh:
                    data = json.load(fh) or {}
        except Exception as exc:  # noqa: BLE001
            self.log("Could not read %s: %s" % (self.state_file, exc),
                     level="WARNING")
        stats = {}
        for entity in self.devices:
            prev = data.get(entity, {})
            stats[entity] = {
                "unavailable_count": prev.get("unavailable_count", 0),
                "total_unavailable_seconds": prev.get(
                    "total_unavailable_seconds", 0.0),
                "unavailable_since": prev.get("unavailable_since"),
                "last_unavailable_ts": prev.get("last_unavailable_ts"),
                "last_available_ts": prev.get("last_available_ts"),
                "reboot_count": prev.get("reboot_count", 0),
                "last_reboot_ts": prev.get("last_reboot_ts"),
            }
        return stats

    def _save_stats(self):
        with self._lock:
            self._save_stats_locked()

    def _save_stats_locked(self):
        out = {e: {k: s.get(k) for k in _PERSIST_FIELDS}
               for e, s in self.stats.items()}
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(out, fh, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as exc:  # noqa: BLE001
            self.log("Could not write %s: %s" % (self.state_file, exc),
                     level="WARNING")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _is_down(self, state):
        return state is None or str(state).lower() in self.down_states

    @staticmethod
    def _iso(epoch):
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    @staticmethod
    def _as_list(value):
        if value is None:
            return []
        return value if isinstance(value, list) else [value]
