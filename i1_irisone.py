"""irisone housekeeping — cross-app chores that belong to no single app (T-66a).

The owner's framing (2026-10-29): an app on the irisone bus that "could do some
housekeeping and be useful for future tasks that are cross-apps but not
strictly belong to one app". It replaces the irisone custom component, whose
only output was `irisone.initilized`, which nothing read.

It does two things.

**The gate test button (IQ-27).** Pressing `input_button.irisone_gate_test`
sends one NON_URGENT and one URGENT test message to every person the presence
app maps, through the same `should_notify` gate the real apps use. With
someone away, the first is dropped and the second arrives: a repeatable live
proof that presence, the target map and the gate agree, after any change.

**Counting the notification gate's decisions** (IQ-28).
Every consumer of notification_policy.should_notify fires `irisone_gate`
events; this app keeps per-day counts per app and outcome, persists them so an
HA or AppDaemon restart does not zero the week, and publishes them as
`irisone.notify_gate_stats`. That entity is how T-69's "one week, no urgent
message dropped" is read from outside docker4: the AppDaemon log endpoint only
returns its last 500 lines.
"""
import json
import os
from datetime import date, timedelta

import appdaemon.plugins.hass.hassapi as hass

import irisone_bus
import notification_policy as policy

DEFAULT_STATE_FILE = "/conf/irisone_state.json"
DEFAULT_HEARTBEAT_S = 300
DEFAULT_TEST_BUTTON = "input_button.irisone_gate_test"
DEFAULT_CHANNEL = "watchdog_alerts"  # T-52: an unnamed channel is discarded by Android
TEST_APP = "irisone_test"
KEEP_DAYS = 14
STATE_VERSION = 1

# notification_policy reason strings -> short counter names.
OUTCOMES = {
    "urgent": "urgent",
    "urgent, recipient away": "urgent_away",
    "recipient home": "home",
    "presence unknown, fail open": "presence_unknown",
    "non-urgent, recipient away": "dropped",
}


def validate_config(args: dict) -> dict:
    """D1: raise ValueError naming the bad key."""
    state_file = args.get("state_file", DEFAULT_STATE_FILE)
    if not isinstance(state_file, str) or not state_file.strip():
        raise ValueError(f"'state_file' must be a non-empty path, got {state_file!r}")
    heartbeat = args.get("heartbeat_s", DEFAULT_HEARTBEAT_S)
    if not isinstance(heartbeat, (int, float)) or isinstance(heartbeat, bool) or not 60 <= heartbeat <= 3600:
        raise ValueError(f"'heartbeat_s' must be between 60 and 3600, got {heartbeat!r}")
    button = args.get("test_button", DEFAULT_TEST_BUTTON)
    if button is not None and (not isinstance(button, str) or not button.startswith("input_button.")):
        raise ValueError(f"'test_button' must be an input_button.* entity or null, got {button!r}")
    channel = args.get("notification_channel", DEFAULT_CHANNEL)
    if not isinstance(channel, str):
        raise ValueError(f"'notification_channel' must be a string, got {channel!r}")
    return {"state_file": state_file, "heartbeat_s": heartbeat, "test_button": button,
            "notification_channel": channel,
            "notification_priority": args.get("notification_priority", "high")}


def gate_test_targets(domain_states: dict | None) -> list:
    """Every notify target the presence app currently maps, sorted, once each."""
    targets = set()
    for entity_id, state in (domain_states or {}).items():
        if entity_id.startswith(f"{irisone_bus.DOMAIN}.presence_") and isinstance(state, dict):
            for t in (state.get("attributes") or {}).get("notify_targets") or []:
                targets.add(irisone_bus.normalise_target(t))
    return sorted(targets)


def record(stats: dict, day: str, event: dict) -> None:
    """Count one gate event into `stats` (mutated): days[day][app][outcome] += 1.

    Drops are also counted per app/key, because the evidence question is "was
    anything dropped that is classed urgent", and that is answered per key.
    A malformed event is counted under app "?" rather than lost silently.
    """
    app = str(event.get("app") or "?")
    outcome = OUTCOMES.get(event.get("reason"), "other")
    per_day = stats.setdefault("days", {}).setdefault(day, {})
    per_app = per_day.setdefault(app, {})
    per_app[outcome] = per_app.get(outcome, 0) + 1
    if outcome == "dropped":
        drops = stats.setdefault("drops_by_key", {}).setdefault(day, {})
        k = f"{app}/{event.get('key', '?')}"
        drops[k] = drops.get(k, 0) + 1


def prune(stats: dict, today: date, keep_days: int = KEEP_DAYS) -> None:
    """Forget days older than keep_days, so the file and the attributes stay small."""
    oldest = (today - timedelta(days=keep_days - 1)).isoformat()
    for section in ("days", "drops_by_key"):
        for day in [d for d in stats.get(section, {}) if d < oldest]:
            del stats[section][day]


def summarise(stats: dict, today: date) -> tuple:
    """(state, attributes) for irisone.notify_gate_stats. State = drops today."""
    last7 = {(today - timedelta(days=i)).isoformat() for i in range(7)}
    week, week_drops = {}, {}
    for day, apps in stats.get("days", {}).items():
        if day not in last7:
            continue
        for app, counts in apps.items():
            agg = week.setdefault(app, {})
            for outcome, n in counts.items():
                agg[outcome] = agg.get(outcome, 0) + n
    for day, keys in stats.get("drops_by_key", {}).items():
        if day in last7:
            for k, n in keys.items():
                week_drops[k] = week_drops.get(k, 0) + n
    today_counts = stats.get("days", {}).get(today.isoformat(), {})
    dropped_today = sum(c.get("dropped", 0) for c in today_counts.values())
    return dropped_today, {"today": today_counts, "last_7_days": week,
                           "drops_by_key_7d": week_drops,
                           "unit_of_measurement": "drops"}


class IrisoneHousekeeping(hass.Hass):
    """Counts notification-gate decisions and publishes irisone.notify_gate_stats."""

    SOURCE = "irisone"
    ENTITY = "irisone.notify_gate_stats"

    def initialize(self):
        cfg = validate_config(self.args)
        self.state_file = cfg["state_file"]
        self.notification_channel = cfg["notification_channel"]
        self.notification_priority = cfg["notification_priority"]
        self.stats = self.load_state()
        if cfg["test_button"]:
            if not self.entity_exists(cfg["test_button"]):
                raise ValueError(f"{cfg['test_button']} does not exist; create it from "
                                 f"deploy/ih_packages/irisone.yaml, or set test_button: null")
            self.listen_state(self._on_test_button, cfg["test_button"])
        self.listen_event(self._on_gate, irisone_bus.GATE_EVENT)
        self.listen_event(self._on_reconnect, "plugin_started")
        self.run_every(self._on_heartbeat, "now+{}".format(cfg["heartbeat_s"]), cfg["heartbeat_s"])
        self.publish()

    def _on_gate(self, event_name, data, kwargs):
        record(self.stats, self.get_now().date().isoformat(), data or {})
        self.save_state()
        self.publish()

    def _on_reconnect(self, event_name, data, kwargs):
        self.publish()

    def _on_test_button(self, entity, attribute, old, new, kwargs):
        self.run_gate_test()

    def run_gate_test(self):
        """One NON_URGENT and one URGENT message per mapped person, through the gate."""
        domain = self.get_state(irisone_bus.DOMAIN)
        epoch = self.get_now().timestamp()
        targets = gate_test_targets(domain)
        if not targets:
            self.log("gate test: no presence entity maps a notify target; is presence running?",
                     level="WARNING")
            return
        for target in targets:
            in_home = irisone_bus.in_home_for_target(domain, target, epoch)
            for urgency in (policy.NON_URGENT, policy.URGENT):
                send, reason = policy.should_notify(urgency, in_home)
                self.log(f"gate test {target} {urgency}: {'SEND' if send else 'DROP'} ({reason})")
                self.fire_event(irisone_bus.GATE_EVENT, **irisone_bus.gate_event(
                    TEST_APP, f"test_{urgency}", target, urgency, send, reason))
                if send:
                    self.call_service(
                        f"notify/{target}",
                        title=f"Gate test: {urgency}",
                        message=f"Sent because: {reason}. in_home={in_home}.",
                        data=self._notification_data(),
                    )

    def _notification_data(self):
        """Companion-app data block (T-52); the same shape as the watchdog's."""
        if not self.notification_channel:
            return {}
        data = {"channel": self.notification_channel}
        if self.notification_priority:
            data["priority"] = self.notification_priority
            data["ttl"] = 0
        return data

    def _on_heartbeat(self, kwargs):
        self.publish()

    def publish(self):
        now = self.get_now()
        prune(self.stats, now.date())
        state, attrs = summarise(self.stats, now.date())
        # str(): AppDaemon 4.5 drops a state of 0 (0 == False) before the HA call,
        # and HA refuses a write with no state (measured live 2026-10-29).
        self.set_state(self.ENTITY, state=str(state),
                       attributes=irisone_bus.build_attributes(
                           self.SOURCE, now, friendly_name="Notification gate stats", **attrs),
                       check_existence=False)

    def load_state(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as e:
            self.log(f"irisone state unreadable, starting fresh: {e}", level="WARNING")
            return {}
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            self.log("irisone state has an unknown shape, starting fresh", level="WARNING")
            return {}
        return data.get("stats", {})

    def save_state(self) -> None:
        """Atomic write, so a crash mid-write cannot corrupt the file."""
        tmp = f"{self.state_file}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": STATE_VERSION, "stats": self.stats}, fh)
            os.replace(tmp, self.state_file)
        except OSError as e:
            self.log(f"irisone could not persist state: {e}", level="ERROR")
