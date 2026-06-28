"""HVAC Watchdog - AppDaemon app.

Monitors a set of Mitsubishi mini-split ``climate.*`` entities (driven by ESP32
boards running the gysmo38/mitsubishi2MQTT firmware) and:

1. **Tracks availability** - counts how often each unit drops to ``unavailable``
   in Home Assistant, how long it stays down, and how many times it has been
   rebooted. Stats persist to ``state.json`` (survives AppDaemon reloads) and are
   optionally republished as ``sensor.*`` entities so they can be graphed.
2. **Auto-recovers via REST** - a unit is considered "needs a reboot" when it
   either (a) stays ``unavailable`` past ``unavailable_grace_seconds``, or
   (b) *flaps* - bouncing to ``unavailable`` ``flap_threshold`` times within
   ``flap_window_seconds`` (a flapper never stays down long enough to trip the
   grace timer, but it is just as broken). The app then logs in to the board's
   web UI and hits ``GET /reboot`` to power-cycle the ESP32. A cooldown plus a
   cap on consecutive reboots prevents reboot loops.

The reboot escalation counter only resets once a unit has been **continuously
online for ``stable_seconds``** - so a flapping unit (whose constant brief
"recoveries" would otherwise reset it forever) keeps escalating until it is
stable or the app gives up.

**Notifications** (optional ``notify_service``) are sent only when action is
taken or needed: after a **successful reboot**, when a reboot **could not be
performed** (board unreachable / HTTP error), and when the app **gives up** after
rebooting ``max_consecutive_reboots`` times without the unit becoming stable.
No notifications are sent for routine unavailable/recovery transitions.

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
        # Flapping: this many unavailable transitions within the window is
        # treated as "needs a reboot" even if it never stays down for `grace`.
        self.flap_threshold = int(self.args.get("flap_threshold", 3))
        self.flap_window = int(self.args.get("flap_window_seconds", 120))
        # A unit must be continuously online this long before its reboot
        # escalation (consecutive count / gave-up) is reset.
        self.stable_seconds = int(self.args.get("stable_seconds", 300))
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
        self._runtime = {
            e: {
                "grace_handle": None,
                "stable_handle": None,
                "cooldown_until": 0.0,
                "consecutive_reboots": 0,
                "gave_up": False,
                "flap_times": [],   # epochs of recent unavailable transitions
            }
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
        for entity in self.devices:
            cur = self.get_state(entity)
            if self._is_down(cur):
                st = self.stats[entity]
                if not st.get("unavailable_since"):
                    st["unavailable_since"] = now
                    st["last_unavailable_ts"] = self._iso(now)
                self._arm_grace(entity)
            self._publish(entity)
        self._save_stats()

        self.log(
            "Initialized. devices=%d grace=%ds cooldown=%ds max_reboots=%d "
            "flap=%d/%ds stable=%ds reboot_enabled=%s notify=%s down_states=%s"
            % (len(self.devices), self.grace_seconds, self.cooldown_seconds,
               self.max_reboots, self.flap_threshold, self.flap_window,
               self.stable_seconds, self.reboot_enabled,
               self.notify_service or "<none>", sorted(self.down_states))
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
            count = st["unavailable_count"]
            self._save_stats_locked()

        rt = self._runtime[entity]
        # It dropped again, so it isn't stable - cancel any pending stability
        # confirmation (don't let a flapper reset its escalation).
        self._cancel_stable(entity)

        # Record this drop for flap detection and prune old entries.
        rt["flap_times"].append(now)
        rt["flap_times"] = [t for t in rt["flap_times"]
                            if now - t <= self.flap_window]
        flaps = len(rt["flap_times"])

        if flaps >= self.flap_threshold:
            self.log("%s is FLAPPING (%d drops in <=%ds, total count=%d) - "
                     "treating as reboot-necessary."
                     % (entity, flaps, self.flap_window, count))
            self._cancel_grace(entity)
            self._maybe_reboot(entity, "flapping")
            return

        self.log("%s went UNAVAILABLE (count=%d). Reboot in %ds if it stays down."
                 % (entity, count, self.grace_seconds))
        self._arm_grace(entity)
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

        # Don't reset the reboot escalation yet - a flapper "recovers"
        # constantly. Only a sustained period of being online (stable_seconds)
        # counts as a real recovery (see _confirm_stable).
        self._cancel_grace(entity)
        self._arm_stable(entity)
        self.log("%s back online (state=%s). Confirming stable for %ds."
                 % (entity, new, self.stable_seconds))
        self._publish(entity)

    def _confirm_stable(self, kwargs):
        entity = kwargs["entity"]
        self._runtime[entity]["stable_handle"] = None
        if self._is_down(self.get_state(entity)):
            return  # dropped again before we got here
        rt = self._runtime[entity]
        if rt["consecutive_reboots"] or rt["gave_up"] or rt["flap_times"]:
            self.log("%s stable for %ds - clearing reboot escalation."
                     % (entity, self.stable_seconds))
        rt["consecutive_reboots"] = 0
        rt["gave_up"] = False
        rt["cooldown_until"] = 0.0
        rt["flap_times"] = []
        self._publish(entity)

    # ------------------------------------------------------------------ #
    # Timers
    # ------------------------------------------------------------------ #
    def _arm_grace(self, entity):
        self._cancel_grace(entity)
        self._runtime[entity]["grace_handle"] = self.run_in(
            self._grace_expired, self.grace_seconds, entity=entity)

    def _cancel_grace(self, entity):
        self._cancel_handle(entity, "grace_handle")

    def _arm_stable(self, entity):
        self._cancel_stable(entity)
        self._runtime[entity]["stable_handle"] = self.run_in(
            self._confirm_stable, self.stable_seconds, entity=entity)

    def _cancel_stable(self, entity):
        self._cancel_handle(entity, "stable_handle")

    def _cancel_handle(self, entity, key):
        handle = self._runtime[entity].get(key)
        if handle is not None:
            try:
                self.cancel_timer(handle)
            except Exception:  # noqa: BLE001
                pass
            self._runtime[entity][key] = None

    def _grace_expired(self, kwargs):
        entity = kwargs["entity"]
        self._runtime[entity]["grace_handle"] = None
        # Recovered while we waited - nothing to do (handler already cleaned up).
        if not self._is_down(self.get_state(entity)):
            return
        self._maybe_reboot(entity, "unavailable")

    # ------------------------------------------------------------------ #
    # Reboot escalation
    # ------------------------------------------------------------------ #
    def _maybe_reboot(self, entity, reason):
        """Gate-keeper: decide whether a reboot is allowed right now, honouring
        cooldown, the give-up flag, and the consecutive-reboot cap."""
        rt = self._runtime[entity]
        if rt["gave_up"]:
            return

        now = time.time()
        if now < rt["cooldown_until"]:
            # Still settling after a reboot; re-check when the cooldown ends.
            remaining = max(1, int(rt["cooldown_until"] - now))
            self._cancel_grace(entity)
            self._runtime[entity]["grace_handle"] = self.run_in(
                self._grace_expired, remaining, entity=entity)
            return

        if not self.reboot_enabled:
            self.log("%s needs a reboot (%s) but reboot_enabled=false."
                     % (entity, reason), level="WARNING")
            return

        if rt["consecutive_reboots"] >= self.max_reboots:
            rt["gave_up"] = True
            name = self.devices[entity]["name"]
            msg = ("HVAC %s (%s) still unstable after %d reboots - manual "
                   "intervention needed." % (name, entity,
                                             rt["consecutive_reboots"]))
            self.log(msg, level="ERROR")
            self._notify(msg)
            self._publish(entity)
            return

        self._do_reboot(entity, reason)

    def _do_reboot(self, entity, reason):
        """Actually send the reboot, update stats, notify, and schedule the
        post-reboot re-check."""
        dev = self.devices[entity]
        name = dev["name"]
        rt = self._runtime[entity]
        host = self._resolve_host(entity)
        now = time.time()
        rt["consecutive_reboots"] += 1
        rt["cooldown_until"] = now + self.cooldown_seconds
        attempt = rt["consecutive_reboots"]

        if not host:
            self.log("%s: no host/IP resolved - cannot reboot (attempt %d/%d)."
                     % (entity, attempt, self.max_reboots), level="WARNING")
            self._notify("HVAC %s (%s) is %s but I can't reboot it: no IP "
                         "resolved. Attempt %d of %d."
                         % (name, entity, reason, attempt, self.max_reboots))
            self._schedule_post_reboot(entity)
            self._publish(entity)
            return

        ok, detail = self._reboot_http(host)
        if ok:
            with self._lock:
                st = self.stats[entity]
                st["reboot_count"] = st.get("reboot_count", 0) + 1
                st["last_reboot_ts"] = self._iso(now)
                self._save_stats_locked()
            self.log("%s: reboot #%d sent to %s (%s, reason=%s). Re-check in %ds."
                     % (entity, attempt, host, detail, reason,
                        self.cooldown_seconds))
            self._notify("HVAC %s (%s) was %s - rebooted it (%s, attempt %d of "
                         "%d)." % (name, entity, reason, host, attempt,
                                   self.max_reboots))
        else:
            self.log("%s: reboot attempt #%d to %s FAILED (%s, reason=%s). "
                     "Re-check in %ds." % (entity, attempt, host, detail, reason,
                                           self.cooldown_seconds),
                     level="WARNING")
            self._notify("HVAC %s (%s) is %s but the reboot FAILED (%s: %s). "
                         "Attempt %d of %d." % (name, entity, reason, host,
                                                detail, attempt, self.max_reboots))
        self._schedule_post_reboot(entity)
        self._publish(entity)

    def _schedule_post_reboot(self, entity):
        """After a reboot, re-check once the cooldown elapses; if still down it
        will reboot again (up to the cap)."""
        self._cancel_grace(entity)
        self._runtime[entity]["grace_handle"] = self.run_in(
            self._grace_expired, self.cooldown_seconds, entity=entity)

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
            self._do_reboot(entity, "manual")

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
