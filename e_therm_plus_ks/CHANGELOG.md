# Changelog

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
