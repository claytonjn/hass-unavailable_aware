# Unavailable-Aware Triggers

State and numeric state triggers for Home Assistant that see through `unavailable` and `unknown`. If a device drops out and comes back unchanged, your automation doesn't fire. If something really changed while it was gone, the automation still fires.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=claytonjn&repository=hass-unavailable_aware&category=integration)

## The problem

A template reload, an integration restart, or a flaky Wi-Fi device takes an entity to `unavailable` (or removes it) for a moment. When it comes back, Home Assistant reports a change from `unavailable` to the value it had all along, and your triggers fire:

- A `numeric_state` trigger with `below: 1` on a template sensor counting people at home fires on `unavailable → 0` after a template reload. "Everyone left" runs again while the house is still empty.
- A `state` trigger with `to: unlocked` on a lock fires when the lock drops off the network and comes back still unlocked. "Welcome home" plays for nobody.
- A `state` trigger with `from: "on"` on a washing machine's power sensor fires on `on → unavailable`. "The laundry is done" arrives mid-cycle when the smart plug drops off Wi-Fi.

The usual fix is a blanket filter: ignore every change from `unavailable` or `unknown`. Home Assistant's purpose-specific triggers (2026.7 and later) and Spook's `state_changed` both work this way. The problem is that a real change made while the entity was unavailable is missed. Leave home while the presence sensor is mid-reconnect, and nothing arms.

Unavailable-Aware triggers remember the last valid state before a gap. Dropping out is never treated as a change. When the entity comes back, they compare the new state with the one from before the gap:

| What happened | Trigger | `unavailable_aware` | Home Assistant's own trigger | Blanket filter |
|---|---|---|---|---|
| `0` → `unavailable` for 0.5 s → `0` (nothing changed) | `to: "0"` | doesn't fire | fires | doesn't fire |
| `1` → `unavailable` for 0.5 s → `0` (it changed while unavailable) | `to: "0"` | **fires**, from `1` | fires, from `unavailable` | **misses it** |
| `on` → `unavailable` (it just dropped out) | `from: "on"` | doesn't fire | fires | doesn't fire |

## Terms

- **Gap**: a stretch of time when the entity has no valid state: it's `unavailable` or `unknown`, or it has been removed from the state machine.
- **Valid state**: any state other than `unavailable` or `unknown`.
- **State before the gap**: the last valid state the entity reported before the gap began.
- **Coming back**: the entity's first valid state after a gap.
- **Raw state**: the previous state Home Assistant reports when the entity comes back: `unavailable`, `unknown`, or nothing after a removal.
- **Bridged gap**: a gap no longer than `bridge_up_to`. When the entity comes back, the trigger compares the new state with the state before the gap, as if the gap never happened.
- **Longer gap**: a gap longer than `bridge_up_to`.
- **Bounce**: coming back unchanged from the state before the gap.

## Installation

1. In HACS, add `https://github.com/claytonjn/hass-unavailable_aware` as a custom repository of type **Integration**, then install **Unavailable-Aware Triggers**.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration** and add **Unavailable-Aware Triggers**.

The triggers only work while the integration is added. Without it, automations using them log an error and a repair lists them.

### Uninstalling

1. Go to **Settings → Devices & services**, open **Unavailable-Aware Triggers**, and delete it. This deletes everything it stored. If any automations or scripts still use its triggers, those stop working and a repair lists them; the repair goes away once you've changed them.
2. Remove it in HACS, then restart Home Assistant.

Do step 1 first: once the files are removed, Home Assistant can't run the integration's clean-up, so its stored data stays behind.

## Usage

To convert a trigger, change its name. Everything else stays the same:

```yaml
triggers:
  # Before: trigger: numeric_state
  - trigger: unavailable_aware.numeric_state
    entity_id: sensor.outdoor_temperature
    below: 0

  # Before: trigger: state
  - trigger: unavailable_aware.state
    entity_id: climate.living_room
    attribute: hvac_action
    to: heating
```

The editor writes the new-style form with `target` and `options`, which works too:

```yaml
triggers:
  - trigger: unavailable_aware.state
    target:
      entity_id: binary_sensor.washing_machine
    options:
      from: "on"
      to: "off"
      for: "00:01:00"
```

Both triggers also work in `wait_for_trigger`.

### `unavailable_aware.state`

Takes the same options as Home Assistant's `state` trigger: `entity_id`, `attribute`, `from`, `not_from`, `to`, `not_to` and `for`. It also takes the gap options below.

A missing or `null` attribute is an ordinary value, not a gap, as in Home Assistant: many attributes are legitimately empty, such as a light's `brightness` while it's off.

Without `from`, `to` or `attribute`, the trigger fires on any change, attributes included, as Home Assistant's does. After a gap, though, it fires only if the state changed: attributes that differ when the entity comes back don't count, because many integrations refresh attributes like signal strength when they reconnect.

`from: unavailable` and `to: unavailable` are forbidden. These triggers never fire into or out of a gap. To react to unavailability, use Home Assistant's own trigger. A leftover `not_from: [unavailable, unknown]` from an old blanket filter is harmless and can stay.

### `unavailable_aware.numeric_state`

Takes the same options as Home Assistant's `numeric_state` trigger: `entity_id`, `attribute`, `above`, `below` (a number or an `input_number`, `number` or `sensor` entity), `value_template` and `for`. It also takes the gap options below.

Whether the value was already in range is judged on the state before the gap. With `below: 20`, `20.1 → gap → 20.3` is not a crossing, and `19 → gap → 18` isn't either, because the value was already in range. Only out-of-range before the gap and in-range after it fires.

### Gap options

| Option | Default | What it does |
|---|---|---|
| `bridge_up_to` | `30` seconds | Sets the longest gap that is bridged. When the entity comes back within this time, the new state is compared with the state before the gap. A gap is measured from when the entity first stopped reporting a valid state, so `on → unavailable → unknown → on` is one gap. |
| `bridge_restarts_for_timer` | `false` | Decides what a bridged gap does to a running `for:` timer when the entity comes back unchanged. `false`: the gap doesn't interrupt the timer, and if the time runs out during the gap, the trigger fires when the entity comes back. `true`: the timer starts over when the entity comes back, as Home Assistant's own trigger does. See [`for:` and gaps](#for-and-gaps). |
| `longer_gaps` | `ignore` | Sets what happens when the entity comes back after a longer gap, or with no state before the gap to compare against (an entity seen for the first time, or one removed from the entity registry or renamed, so a re-paired device reusing an entity ID is never compared with the old one). `ignore` doesn't fire; a `for:` timer that was already running starts over if the entity comes back still matching. `home_assistant_default` fires as Home Assistant's own trigger would, treating the change as coming from `unavailable`, `unknown` or a removed entity (see below). |

#### `for:` and gaps

What a gap does to a running `for:` timer:

| The entity comes back… | The timer |
|---|---|
| from a bridged gap, unchanged | Keeps running (`bridge_restarts_for_timer: false`), or starts over (`true`). |
| from a bridged gap, changed | Ends. The change is judged like any other and may start a new timer. |
| from a longer gap, with `longer_gaps: ignore`, still matching | Starts over. "Still matching" is judged against the state before the gap, never `unavailable`. The change that started the timer was seen before the gap, and the trigger only fires once the whole `for:` has been seen after it, so nothing is assumed about the gap. To keep timers running through longer gaps, raise `bridge_up_to`. |
| from a longer gap, with `longer_gaps: ignore`, not matching | Ends. |
| from a longer gap, with `longer_gaps: home_assistant_default` | As in Home Assistant: the timer ends, and coming back is judged as a change from `unavailable`, which may start a new timer. |

#### Choosing `bridge_up_to` and `longer_gaps`

During a long gap you can't tell *when* the change happened. A cloud camera syncing after an hour offline, or a button press delivered late, would run its automation long after the moment has passed. Raise `bridge_up_to` only where a late action is better than none. Ignoring longer gaps matches Home Assistant's purpose-specific triggers once the bridge period has passed.

Use `longer_gaps: home_assistant_default` where missing a change is worse than running twice, such as departure or arming automations. Past `bridge_up_to`, the trigger then behaves like Home Assistant's own:

- `to: "on"` fires on `unavailable → on`, even if the entity was already `on` before the gap. A bounce after a long gap fires again.
- `from:` is matched against the raw state, so `from: "off"` never matches the entity coming back: as far as Home Assistant is concerned, it came from `unavailable`.
- `trigger.from_state` is the raw state, as in Home Assistant. `trigger.gap` is still set.
- `for:` counts from when the entity comes back.

It applies only when the entity comes back. Going *into* a gap never fires (`from: "on"` on `on → unavailable`), because at that moment there's no way to know how long the gap will last.

## Trigger variables

Every variable Home Assistant's own trigger sets is also set by Unavailable-Aware triggers, so existing templates keep working, except ones that check `trigger.platform`. Differences listed below:

| Variable | Value |
|---|---|
| `trigger.platform` | `unavailable_aware.state` or `unavailable_aware.numeric_state`, not `state` or `numeric_state`. Home Assistant sets it to the trigger's own name, as it does for its own purpose-specific triggers, so a template that checks `trigger.platform == 'state'` needs to check the new name. |
| `trigger.from_state` | The state **before the gap**: what the entity last said before it dropped out. Templates like `trigger.from_state.state` compare against the real previous value, not `unavailable`. After a longer gap with `longer_gaps: home_assistant_default`, this is the raw state instead, as in Home Assistant. |
| `trigger.to_state` | The state the entity came back with. Its `last_changed` is when the entity came back, not when the change happened. Its `context` is populated from the entity coming back, so checking `context.user_id` or `context.parent_id` to tell a manual change from an automated one doesn't work after a gap. |
| `trigger.raw_from_state` | What Home Assistant actually reported: the `unavailable` or `unknown` state, or `None` after a removal. |
| `trigger.gap` | `None` for an ordinary change. After a gap: `kind` (how the gap started: `unavailable`, `unknown`, `removed`, or `restart` when Home Assistant or the integration restarted during it), `started`, `ended` and `duration` (seconds). |

## Traces

When these triggers suppress something Home Assistant's own trigger would have fired for, the automation gets a trace marked **not triggered**, with a reason:

- `bridged_gap`: the entity came back unchanged (or not matching `from`/`to`) from a gap it was compared across.
- `longer_gap_ignored`: it came back from a gap longer than `bridge_up_to`, and `longer_gaps` is `ignore`.
- `previous_unknown`: it came back, there's no state from before the gap to compare with (see below), and `longer_gaps` is `ignore`.
- `entered_gap`: Home Assistant would have fired going *into* the gap (e.g. `from: "on"` on `on → unavailable`), and coming back didn't fire either.

Each gap produces at most one such trace, written when the entity comes back. The trace data shows the state before the gap, the raw state, the state the entity came back with, and the gap details.

## Edge cases

- **Round trips inside a gap are invisible.** `X → (Y while unavailable) → X` looks like nothing happened, because nothing was reported. Don't use these triggers where every transition must be seen, such as inputs to a presence or security model.
- **Collapsed sequences fire once.** A counter going `3 → gap → 10` fires one change from 3 to 10, not seven.
- **Leaving-edge waits.** A real change during a gap is only seen when the entity comes back. If a `wait_for_trigger` times out while the gap is still open, the wait ends without a trigger. Decide in the automation what that means. `states('sensor.x') in ['unavailable', 'unknown']` tells you the gap is still open.
- **Event entities.** The state is the time Home Assistant received the last event, and it's restored when the entity is re-added, so a reload or restart comes back with the same time and doesn't fire. A different time after a gap means the integration reported an event during the gap or as the entity came back. The state says when Home Assistant received it, which can be later than the actual press if the device delivered it late. After a longer gap, `longer_gaps` decides.

## When you shouldn't use Unavailable-Aware triggers

Keep Home Assistant's own triggers when you explicitly want to trigger on a reconnect:

- Alerts for a device going offline or coming back (`to: unavailable`, "restored" notifications).
- Safety alarms where a duplicate alert is better than a suppressed one.
- Anything that must follow a device's state immediately even when the timing is unknown, such as syncing modes.

## How it works

The integration remembers the last valid state of each entity its triggers use, and keeps it through automation reloads and Home Assistant restarts. If Home Assistant crashes, the last few seconds of changes may not be saved; at worst, an automation runs when it didn't need to.

To see what's remembered, download the integration's diagnostics.
