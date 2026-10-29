"""Presence publisher — who is home, published once on the irisone bus (T-70).

Why this exists
---------------
T-69: apps drop non-urgent notifications for a recipient who is not home. Each
app re-deriving "is this person home" from raw trackers would be eight copies of
the same judgement, each wrong in its own way (the old per-app `tracker` gate
dropped silently on `unknown` and on every 2-minute GPS blip). This app makes the
judgement once and publishes it as `irisone.presence_<key>`.

The rules (owner decisions 2026-10-29, docs/planning/T-69-interview.md)
---------------------------------------------------------------------
* Source of truth is `person.*` (HA merges the person's trackers).
* Only `zone.home` counts as home (IQ-08). Any other zone, or `not_home`, is away.
* Leaving is held for `away_debounce_s` (180 s, IQ-07): 14 of 35 measured away
  episodes in a week were <= 2 min flips at the edge of the 100 m home circle.
  Arriving is published at once.
* A tracker not updated for `stale_after_s` (6 h, IQ-04) is `unknown`: HA keeps
  a dead phone's last fix forever, and that must not read as "away".
* `unknown` is published at once. Consumers send on unknown, so it can only add
  notifications, never lose one.
* The notify-target -> person map lives here, once (IQ-09), and is published as
  the `notify_targets` attribute so consumers never hold a copy.

The pure functions below carry all of that and are tested directly; the app
class only reads states, calls them, and writes.
"""
import re

HOME = "home"
AWAY = "away"
UNKNOWN = "unknown"

DEFAULT_DEBOUNCE_S = 180
DEFAULT_HEARTBEAT_S = 300
DEFAULT_STALE_AFTER_S = 6 * 3600

_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_NOT_REPORTING = frozenset({"unavailable", "unknown", "none", ""})


def _number(args: dict, key: str, default, minimum, maximum=None):
    value = args.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number of seconds, got {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"'{key}' must be {bound}, got {value!r}")
    return value


def validate_config(args: dict) -> dict:
    """Return the validated settings, or raise ValueError naming the bad key (D1)."""
    persons = args.get("persons")
    if not isinstance(persons, list) or not persons:
        raise ValueError(f"'persons' must be a non-empty list, got {persons!r}")

    seen_keys, seen_targets, out = set(), {}, []
    for i, p in enumerate(persons):
        if not isinstance(p, dict):
            raise ValueError(f"persons[{i}] must be a mapping, got {p!r}")
        entity = p.get("person")
        if not isinstance(entity, str) or not entity.startswith("person.") or entity == "person.":
            raise ValueError(f"persons[{i}].person must be a person.* entity id, got {entity!r}")
        key = p.get("key")
        if not isinstance(key, str) or not _KEY.match(key):
            raise ValueError(f"persons[{i}].key must match {_KEY.pattern}, got {key!r}")
        if key == "anyone_home":
            raise ValueError(f"persons[{i}].key 'anyone_home' is reserved")
        if key in seen_keys:
            raise ValueError(f"persons[{i}].key {key!r} is used twice")
        seen_keys.add(key)
        notify = p.get("notify")
        if isinstance(notify, str):
            notify = [notify]
        if not isinstance(notify, list) or not notify:
            raise ValueError(f"persons[{i}].notify must be a non-empty list, got {notify!r}")
        for t in notify:
            if not isinstance(t, str) or not t.strip():
                raise ValueError(f"persons[{i}].notify entries must be non-empty strings, got {t!r}")
            if t.startswith(("notify.", "notify/")):
                raise ValueError(f"persons[{i}].notify takes the service name without "
                                 f"the domain: use {t[7:]!r}, not {t!r}")
            if t in seen_targets:
                raise ValueError(f"notify target {t!r} is listed for both "
                                 f"{seen_targets[t]!r} and {key!r}")
            seen_targets[t] = key
        out.append({"person": entity, "key": key, "notify": list(notify)})

    return {
        "persons": out,
        "away_debounce_s": _number(args, "away_debounce_s", DEFAULT_DEBOUNCE_S, 0),
        "heartbeat_s": _number(args, "heartbeat_s", DEFAULT_HEARTBEAT_S, 60, 3600),
        "stale_after_s": _number(args, "stale_after_s", DEFAULT_STALE_AFTER_S, 60),
    }


def observe(person_state, source_age_s, stale_after_s: float) -> str:
    """What the person's raw state says right now: HOME, AWAY or UNKNOWN.

    `person_state` is the person entity's state string. `source_age_s` is how long
    ago the person's winning tracker was last updated, or None if that cannot be
    read -- in which case staleness cannot be judged and the state is taken as is.
    """
    if person_state is None or str(person_state).strip().lower() in _NOT_REPORTING:
        return UNKNOWN
    if source_age_s is not None and source_age_s >= stale_after_s:
        return UNKNOWN
    return HOME if person_state == HOME else AWAY


def next_presence(observed: str, published, pending_since, now: float,
                  debounce_s: float) -> tuple:
    """Return (state to publish, pending_since) for one person.

    Only HOME -> AWAY is held. AWAY with no prior HOME evidence (fresh start, or
    coming out of UNKNOWN) publishes at once: there is no blip to absorb.
    """
    if observed != AWAY:
        return observed, None
    if published != HOME:
        return AWAY, None
    if pending_since is None:
        pending_since = now
    if now - pending_since >= debounce_s:
        return AWAY, None
    return HOME, pending_since


def anyone_home(states: list) -> str:
    """Household value: 'on' if anyone is home, 'off' only if everyone is known away."""
    if HOME in states:
        return "on"
    if states and all(s == AWAY for s in states):
        return "off"
    return UNKNOWN


def in_home(state: str):
    """The `in_home` attribute consumers read: True, False, or None for unknown."""
    return {HOME: True, AWAY: False}.get(state)
