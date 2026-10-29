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

## Gate counters

Each app that calls `notification_policy.should_notify` reports the outcome with
`self.fire_event(irisone_bus.GATE_EVENT, **irisone_bus.gate_event(...))`.
`irisone.notify_gate_stats` has state = drops today, and attributes `today`,
`last_7_days` (per app and outcome) and `drops_by_key_7d`. Read
`drops_by_key_7d` against the T-72 classification: a key there that is classed
urgent is a bug.

## Failure posture

- Bad config raises at `initialize()` (D1); the watchdog reports the failed app.
- The entities vanish when Home Assistant restarts, and come back when AppDaemon re-runs
  `initialize()`. Readers treat missing, stale or unknown as `None`, and the
  notification gate sends on `None`. Stopping these apps therefore turns every gate
  back into "always send" after 15 minutes. That is the rollback.

## Deploy checklist (operator)

1. Create the GitHub remote `the-louie/appdaemon_irisone` (private) and push.
2. On docker4, clone into the AppDaemon apps directory next to the other app repos.
3. Create `config.yaml` from `config.yaml.example` (both sections).
4. Restart AppDaemon (a new app directory is not hot-loaded).
5. Verify:
   - `GET /api/states/irisone.presence_anders` matches `person.anders_green`, and `updated`
     moves forward between two reads 5 minutes apart.
   - The AppDaemon log has one `presence anders: None -> home (initialize ...)` line and no
     WARNING about a missing entity.
   - After an HA restart, the entities are back within seconds.
   - `GET /api/states/irisone.notify_gate_stats` exists with state `0`.
6. Add `presence` and `irisone` to the watchdog's `expect_running`.

The irisone custom component is **not** removed at this step. Claude advises when.
