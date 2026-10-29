# appdaemon_irisone

The `irisone.*` bus: shared facts that several AppDaemon apps need, published once
by this repo instead of being re-derived in every app. The convention is in
`/code/hass/PROJECT_RULES.md`, section "The `irisone.*` bus".

Two apps live here:

| class | module | publishes | ticket |
|---|---|---|---|
| `PresencePublisher` | `i1_presence.py` | `irisone.presence_<key>`, `irisone.presence_anyone_home` | T-70 |
| `IrisoneHousekeeping` | `i1_irisone.py` | `irisone.notify_gate_stats`, the gate test button | T-66a |

`irisone_bus.py` is the shared reader/writer helper. It has no AppDaemon import, and
consumer apps carry byte-identical copies, guarded by
`tests/test_policy_copies_match.py`. Edit the copy in this repo, then re-copy.

## Failure posture

- Bad config raises at `initialize()` (D1); the watchdog reports the failed app.
- The entities vanish when Home Assistant restarts, and come back when AppDaemon re-runs
  `initialize()`. Readers treat missing, stale or unknown as `None`, and the
  notification gate sends on `None`. Stopping these apps therefore turns every gate
  back into "always send" after 15 minutes. That is the rollback.

## Deploy checklist (operator)

Filled in as the apps land; see the end of Sprint 14.
