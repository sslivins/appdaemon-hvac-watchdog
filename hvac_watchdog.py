"""HVAC Watchdog - AppDaemon app.

Monitors a set of Mitsubishi mini-split ``climate.*`` entities (driven by ESP32
boards running either the legacy gysmo38/mitsubishi2MQTT firmware or the newer
sslivins/mitsubishi-heatpump firmware) and:

1. **Tracks availability** - counts how often each unit drops to ``unavailable``
   in Home Assistant, how long it stays down, and how many times it has been
   rebooted. Stats persist to ``state.json`` (survives AppDaemon reloads) and are
   optionally republished as ``sensor.*`` entities so they can be graphed.
2. **Auto-recovers via REST** - a unit is rebooted when it stays ``unavailable``
   past ``unavailable_grace_seconds`` (the firmware sometimes gets *stuck*
   unavailable; a power-cycle fixes that). The app logs in to the board's web UI
   and hits ``GET /reboot`` to power-cycle the ESP. A cooldown plus a cap on
   consecutive reboots prevents reboot loops.
3. **Flap detection (notify-only)** - a unit that *bounces* to ``unavailable``
   ``flap_threshold`` times within ``flap_window_seconds`` is **not** rebooted
   (flapping is a Wi-Fi/power problem a reboot doesn't fix). Instead the app
   sends a single, **rate-limited** heads-up (at most once per
   ``flap_notify_interval_seconds`` per device, default 24 h). A flapper still
   gets the normal grace timer, so if it stops bouncing and stays down it is
   still rebooted as a sustained outage.

The reboot escalation counter only resets once a unit has been **continuously
online for ``stable_seconds``**.

**Notifications** (optional ``notify_service``) are sent only when action is
taken or needed: after a **successful reboot**, when a reboot **could not be
performed** (board unreachable / HTTP error), when the app **gives up** after
rebooting ``max_consecutive_reboots`` times, and a rate-limited heads-up when a
unit is **flapping**. No notifications are sent for routine unavailable/recovery
transitions.

Two firmware flavours are supported, selected per-device via ``firmware:``
(default ``mitsubishi2mqtt``):

* ``mitsubishi2mqtt`` (legacy) - the web UI uses cookie auth with a hard-coded
  username ``admin`` and a static session cookie ``M2MSESSIONID=1`` set on a
  successful ``POST /login``. We both POST the login form and set the cookie
  explicitly, then ``GET /reboot``. Version is scraped from the root HTML page.
* ``heatpump`` (sslivins/mitsubishi-heatpump) - a JSON REST API. Reboot is
  ``POST /api/system/restart``; version comes from ``GET /api/status`` (the
  ``version`` field). When the board has web auth enabled, requests carry an
  ``X-API-Key`` header (per-device ``api_key:``); with auth disabled the API is
  open and no key is needed.

The firmware-update check compares each unit against the latest GitHub release
of *its* firmware repo (``gysmo38/mitsubishi2MQTT`` or
``sslivins/mitsubishi-heatpump`` by default; overridable per firmware).

No entity IDs, hosts, or secrets are hard-coded - everything comes from the
app's YAML config (with passwords/keys supplied via ``!secret``).
"""

import json
import os
import re
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

# Supported firmware flavours -> default GitHub repo for the update check.
_DEFAULT_FW_REPOS = {
    "mitsubishi2mqtt": "gysmo38/mitsubishi2MQTT",
    "heatpump": "sslivins/mitsubishi-heatpump",
}


class HvacWatchdog(hass.Hass):

    def initialize(self):
        # --- Config -----------------------------------------------------
        self.username = str(self.args.get("username", "admin"))
        self.password = str(self.args.get("password", ""))
        self.grace_seconds = int(self.args.get("unavailable_grace_seconds", 300))
        self.cooldown_seconds = int(self.args.get("reboot_cooldown_seconds", 300))
        self.max_reboots = int(self.args.get("max_consecutive_reboots", 3))
        # Flapping: this many unavailable transitions within the window is
        # treated as "flapping". Flappers are NOT rebooted (a reboot doesn't fix
        # a Wi-Fi/power problem) - they only trigger a rate-limited notification.
        self.flap_threshold = int(self.args.get("flap_threshold", 3))
        self.flap_window = int(self.args.get("flap_window_seconds", 120))
        # At most one "is flapping" notification per device per this interval.
        self.flap_notify_interval = int(
            self.args.get("flap_notify_interval_seconds", 86400))
        self.flap_notify_enabled = bool(self.args.get("flap_notify_enabled", True))
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

        # --- Firmware update check (optional) ---------------------------
        fw = self.args.get("firmware_check", {}) or {}
        self.fw_enabled = bool(fw.get("enabled", False))
        # Per-firmware GitHub repos. Back-compat: a bare `repo:` overrides the
        # legacy mitsubishi2mqtt repo; `repos:` is a {firmware: repo} mapping.
        self.fw_repos = dict(_DEFAULT_FW_REPOS)
        if fw.get("repo"):
            self.fw_repos["mitsubishi2mqtt"] = str(fw["repo"])
        for k, v in (fw.get("repos", {}) or {}).items():
            self.fw_repos[str(k).strip().lower()] = str(v)
        self.fw_time = fw.get("check_time", "09:00:00")
        # Weekday to run on (mon..sun); null/omitted = every day.
        self.fw_day = self._parse_weekday(fw.get("check_day", "sun"))

        # --- Devices ----------------------------------------------------
        # Each: {entity, host, [tracker], [name], [firmware], [api_key]}.
        # `host` is the static IP; optional `tracker` (a device_tracker) is
        # consulted for a live `ip` attribute first (DHCP resilience), falling
        # back to `host`. `firmware` selects the reboot/version protocol
        # ("mitsubishi2mqtt" default, or "heatpump"); `api_key` is sent as
        # X-API-Key to a heatpump board that has web auth enabled.
        self.devices = {}
        for d in self._as_list(self.args.get("devices", [])):
            entity = d["entity"]
            fw_type = str(d.get("firmware", "mitsubishi2mqtt")).strip().lower()
            if fw_type not in _DEFAULT_FW_REPOS:
                self.log("Device %s: unknown firmware '%s' - defaulting to "
                         "mitsubishi2mqtt." % (entity, fw_type), level="WARNING")
                fw_type = "mitsubishi2mqtt"
            self.devices[entity] = {
                "entity": entity,
                "host": d.get("host"),
                "tracker": d.get("tracker"),
                "name": d.get("name") or entity.split(".")[-1],
                "firmware": fw_type,
                "api_key": d.get("api_key"),
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
                "flap_last_notify": 0.0,  # epoch of last flap notification
            }
            for e in self.devices
        }

        # --- Wiring -----------------------------------------------------
        for entity in self.devices:
            self.listen_state(self._on_state, entity)

        # Manual reboot trigger for testing: fire event "hvac_watchdog_reboot"
        # with data {"entity": "climate.master_bedroom"} (or "all").
        self.listen_event(self._on_manual_reboot, "hvac_watchdog_reboot")

        # Weekly firmware-update check (optional). run_daily fires every day at
        # the time; the callback no-ops on days other than fw_day.
        if self.fw_enabled:
            self.run_daily(self._firmware_check, self.parse_time(self.fw_time))
        # Manual firmware check for testing: fire event "hvac_watchdog_fwcheck".
        self.listen_event(self._on_manual_fwcheck, "hvac_watchdog_fwcheck")

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
            "flap=%d/%ds(notify-only,<=1/%ds) stable=%ds reboot_enabled=%s "
            "notify=%s fw_check=%s down_states=%s"
            % (len(self.devices), self.grace_seconds, self.cooldown_seconds,
               self.max_reboots, self.flap_threshold, self.flap_window,
               self.flap_notify_interval, self.stable_seconds,
               self.reboot_enabled, self.notify_service or "<none>",
               ("%s@%s day=%s" % (self.fw_repo, self.fw_time, self.fw_day))
               if self.fw_enabled else "off",
               sorted(self.down_states))
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
            # Flapping is a Wi-Fi/power problem a reboot won't fix, so we only
            # notify (rate-limited) and never reboot a flapper. It still gets the
            # normal grace timer below, so a flapper that stops bouncing and
            # stays down is still rebooted as a sustained outage.
            self.log("%s is FLAPPING (%d drops in <=%ds, total count=%d) - "
                     "notify only, not rebooting."
                     % (entity, flaps, self.flap_window, count), level="WARNING")
            self._maybe_notify_flap(entity, flaps)

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
        # Start flap detection fresh after a reboot so stale pre-reboot drops
        # don't immediately re-trigger once the cooldown ends.
        rt["flap_times"] = []
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

        ok, detail = self._reboot_http(host, dev)
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

    def _reboot_http(self, host, dev):
        """Dispatch a reboot to the right firmware protocol. Returns (ok, detail)."""
        if dev.get("firmware") == "heatpump":
            return self._reboot_heatpump(host, dev)
        return self._reboot_m2m(host)

    def _reboot_m2m(self, host):
        """mitsubishi2MQTT: POST /login then GET /reboot. Returns (ok, detail)."""
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

    def _reboot_heatpump(self, host, dev):
        """sslivins/mitsubishi-heatpump: POST /api/system/restart. Sends the
        X-API-Key header when an api_key is configured (only needed if the board
        has web auth enabled). Returns (ok, detail)."""
        headers = {"User-Agent": "hvac_watchdog"}
        if dev.get("api_key"):
            headers["X-API-Key"] = dev["api_key"]
        try:
            r = requests.post("http://%s/api/system/restart" % host,
                              headers=headers, timeout=self.http_timeout)
            # The board restarts ~500ms after responding; 2xx means accepted.
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

    def _maybe_notify_flap(self, entity, flaps):
        """Send a flapping heads-up, rate-limited to at most one per
        ``flap_notify_interval`` seconds per device. Flappers are never
        rebooted, so this is the only signal that a unit is bouncing."""
        rt = self._runtime[entity]
        now = time.time()
        if not self.flap_notify_enabled:
            return
        if now - rt.get("flap_last_notify", 0.0) < self.flap_notify_interval:
            return
        rt["flap_last_notify"] = now
        name = self.devices[entity]["name"]
        mins = max(1, self.flap_window // 60)
        self._notify(
            "HVAC %s (%s) is FLAPPING (%d drops in ~%d min) - likely a "
            "Wi-Fi/power issue, not rebooting. Worth checking the unit."
            % (name, entity, flaps, mins))

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
    # Firmware update check
    # ------------------------------------------------------------------ #
    def _on_manual_fwcheck(self, event_name, data, kwargs):
        self.log("Manual firmware check requested.")
        self._firmware_check({"force": True})

    def _firmware_check(self, kwargs):
        # run_daily fires every day; only proceed on the configured weekday
        # (unless this was a manual/forced check or no day is configured).
        if (not kwargs.get("force") and self.fw_day is not None
                and self.datetime().weekday() != self.fw_day):
            return

        latest_by_repo = {}   # repo -> (tag, ver_tuple), or None if fetch failed
        behind, unreadable = [], []
        for entity, dev in self.devices.items():
            repo = self.fw_repos.get(dev["firmware"])
            if not repo:
                continue
            if repo not in latest_by_repo:
                tag = self._github_latest_tag(repo)
                latest_by_repo[repo] = (tag, self._ver_tuple(tag)) if tag else None
                if latest_by_repo[repo] is None:
                    self.log("Firmware check: couldn't fetch latest release from "
                             "%s." % repo, level="WARNING")
            latest = latest_by_repo[repo]
            if latest is None:
                continue

            host = self._resolve_host(entity)
            ver = self._read_installed_version(host, dev) if host else None
            if not ver:
                unreadable.append(dev["name"])
                continue
            if self._ver_tuple(ver) < latest[1]:
                behind.append((dev["name"], ver, latest[0], repo))

        if behind:
            lst = ", ".join(
                "%s (on %s -> %s, https://github.com/%s/releases)"
                % (n, v, lt, repo) for n, v, lt, repo in behind)
            msg = "Mitsubishi HVAC firmware update available. Behind: %s." % lst
            self.log(msg)
            self._notify(msg)
        else:
            self.log("Firmware check: all readable units up to date.")
        if unreadable:
            self.log("Firmware check: couldn't read version for: %s."
                     % ", ".join(unreadable), level="WARNING")

    def _github_latest_tag(self, repo):
        url = "https://api.github.com/repos/%s/releases/latest" % repo
        try:
            r = requests.get(url, timeout=self.http_timeout,
                             headers={"Accept": "application/vnd.github+json",
                                      "User-Agent": "hvac_watchdog"})
            if r.status_code == 200:
                return r.json().get("tag_name")
            self.log("Firmware check: GitHub API HTTP %d for %s."
                     % (r.status_code, repo), level="WARNING")
        except (requests.RequestException, ValueError) as exc:
            self.log("Firmware check: GitHub API error for %s: %s"
                     % (repo, type(exc).__name__), level="WARNING")
        return None

    def _read_installed_version(self, host, dev):
        """Read the running firmware version, using the device's protocol."""
        if dev.get("firmware") == "heatpump":
            return self._read_version_heatpump(host, dev)
        return self._read_version_m2m(host)

    def _read_version_m2m(self, host):
        """mitsubishi2MQTT: scrape the version off the web UI root page."""
        base = "http://%s" % host
        try:
            s = requests.Session()
            s.cookies.set("M2MSESSIONID", "1")
            r = s.get(base + "/", timeout=self.http_timeout, allow_redirects=True)
            m = re.search(r"\d{4}\.\d+\.\d+", r.text)
            if m:
                return m.group(0)
        except requests.RequestException:
            pass
        return None

    def _read_version_heatpump(self, host, dev):
        """heatpump: GET /api/status returns JSON with a "version" field."""
        headers = {"User-Agent": "hvac_watchdog"}
        if dev.get("api_key"):
            headers["X-API-Key"] = dev["api_key"]
        try:
            r = requests.get("http://%s/api/status" % host, headers=headers,
                             timeout=self.http_timeout)
            if r.status_code == 200:
                return r.json().get("version")
        except (requests.RequestException, ValueError):
            pass
        return None

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
    def _parse_weekday(value):
        """Map a weekday name (mon..sun) to 0..6. None/unknown -> None (= any
        day)."""
        if value is None:
            return None
        days = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5,
                "sun": 6}
        return days.get(str(value).strip().lower()[:3])

    @staticmethod
    def _ver_tuple(s):
        """Turn a version string like '2024.8.0' into a comparable int tuple."""
        parts = re.findall(r"\d+", str(s))
        return tuple(int(p) for p in parts) if parts else (0,)

    @staticmethod
    def _as_list(value):
        if value is None:
            return []
        return value if isinstance(value, list) else [value]
