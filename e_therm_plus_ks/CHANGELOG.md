# Changelog

## 2.6.209

- Add live updates to guest thermostat room pages so card color, status, temperature, and setpoint align automatically with real thermostat changes.
- Use neutral gray cards when off, orange when heating, and blue when cooling.

## 2.6.208

- Show thermostat runtime status badges on guest room cards: `OFF`, `HEAT ON`, or `COOL ON`.

## 2.6.207

- Remove the back-arrow buttons from the `Termostati Guest` room list and room detail pages.

## 2.6.206

- Add manual guest assignment fields to the vTherm thermostat editor: `guest_enabled` and `guest_room`.
- Make the guest thermostat page include only manually flagged thermostats instead of automatically including every `SUITE PLAN` thermostat.

## 2.6.205

- Add the `Termostati Guest` entry to the actual main home menu, linking to the guest room/suite thermostat pages.

## 2.6.204

- Move guest thermostats out of the normal `Termostati` page and add a dedicated `Termostati Guest` entry/page for suite and room thermostats.
- Filter live thermostat updates by page so normal and guest lists stay separated during SSE/API refreshes.

## 2.6.203

- Separate guest thermostats from the normal technical thermostat list. Thermostats configured on `SUITE PLAN` or marked as guest/suite/hotel are rendered under a dedicated `TERMOSTATI GUEST` section.

## 2.6.202

- Embed the thermostat configuration directly in the detail page JavaScript and use it as the stable fallback for climate min/max bounds, so the setpoint knob cannot fall back to the generic `5-35` range when live snapshots omit config metadata.

## 2.6.201

- Keep the thermostat setpoint knob hidden during the embedded first render and reveal it only after the first live `/api/entities` or SSE snapshot, preventing visible jumps when initial and live temperature ranges differ.
- Trigger an immediate detail-page snapshot fetch on open instead of waiting for the periodic refresh.

## 2.6.200

- Render the thermostat setpoint knob with its initial server-side dial position, avoiding the first-frame jump when opening a thermostat detail page.
- Use the same percentage-based dial radius in HTML and JavaScript so the initial render and live updates do not reposition the knob.

## 2.6.199

- Use a numeric-only initial setpoint value for JavaScript knob positioning instead of reusing the localized display string.
- Position the thermostat setpoint knob immediately after wiring the ring, before the full live render pass.

## 2.6.198

- Hide the thermostat setpoint knob until JavaScript has positioned it from the current value, preventing the visible jump from its default DOM position.
- Remove the duplicate knob `pointerdown` listener and let the ring wrapper handle pointer capture consistently.

## 2.6.197

- Map the thermostat setpoint knob over a 330 degree dial arc instead of a full 360 degree circle, so minimum and maximum setpoints no longer share the same screen position.
- Apply the same arc mapping to current-temperature tick placement and drag calculations, reducing jumps across thermostats with different temperature ranges.

## 2.6.196

- Make thermostat dial dragging incremental from the previous pointer position, preventing jumps when the pointer crosses the circular range seam.
- Keep the center setpoint label owned by the active drag preview while dragging.

## 2.6.195

- Stabilize thermostat detail setpoint drag: live SSE updates no longer move the dial knob while the user is dragging it.
- Normalize knob grab angle so dragging from the bubble does not jump across the circular range boundary.
- Ignore empty retained `/set` MQTT command cleanup messages without logging a warning.

## 2.6.194

- Keep `OUT_STATUS` owned by the virtual thermostat demand/PWM calculation, so `ha_multi_sensor_avg` thermostats do not get forced back to `OFF` by real switch readback.
- Align thermostat list and detail ON/OFF display: active `DEMAND_ON` or PWM now shows ON/heat before falling back to real switch state.

## 2.6.193

- Read mapped `real_targets.power_switch` state from Home Assistant and expose it as `REAL_POWER_SWITCH_STATE`.
- Make the thermostat page follow the real power switch state when available: switch on means ON/orange, switch off means OFF/gray.

## 2.6.192

- Restore thermostat dial color semantics: gray when there is no heat/cool demand, orange only for active heat demand, blue only for active cool demand.

## 2.6.191

- Keep the thermostat dial accented by active season: orange for winter/heat and blue for summer/cool, while the relay badge still shows real ON/OFF demand.
- Update `OUT_STATUS` from computed demand so heat requests show `HEAT ON` instead of `OFF` when the virtual thermostat is actively calling for heat.
- Let computed `DEMAND_ON=ON` take precedence over an idle real HVAC action in the thermostat page display.

## 2.6.190

- Show flat `real_targets.power_switch` in the Heat relay field when reopening non-split thermostats in the editor.

## 2.6.189

- Fix vTherm editor real relay mapping for non-split `ha_multi_sensor_avg` thermostats.
- Save the Heat relay field as flat `real_targets.power_switch` when seasonal split outputs are disabled.

## 2.6.188

- Metadata-only bump to force a fresh Home Assistant Supervisor store refresh after adding the add-on scoped changelog.
- Keep `CHANGELOG.md` inside the add-on folder next to `config.yaml`.

## 2.6.187

- Add optional `source.helper_climate_entity_id` for `ha_multi_sensor_avg` thermostats.
- Use the helper climate as Home Assistant setpoint/mode memory while keeping averaged probe temperature as the virtual thermostat temperature.
- Add the helper climate field to the debug thermostat editor.

## 2.6.186

- Add add-on changelog for Home Assistant Supervisor update details.
- Add repository URL to the add-on manifest.
- Bump add-on version to force a fresh store update check.

## 2.6.185

- Allow `ha_multi_sensor_avg` virtual thermostats without a real `climate.xxx` thermostat.
- Keep `outputs.power` active for sensor-average thermostats so demand can drive groups and consensus switches.
- Save `real_thermostat` only when a real climate entity is configured.

## 2.6.184

- Add `pwm_min_active`, default `15`.
- Clamp active PWM requests below the minimum to the configured minimum while keeping OFF at `0`.

## 2.6.183

- Add stable spacing between the PWM meter and `COOL ON` / `HEAT ON` status badges.

## 2.6.182

- Show live PWM percentage and visual meter on thermostat cards.
- Publish `THERM.PWM` into realtime thermostat state.
