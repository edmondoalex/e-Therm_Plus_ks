# Session Summary - 2026-04-29

## Contesto
- Dopo gli aggiornamenti, l'addon mostrava reconnect MQTT ripetuti (`mqtt_not_connected`), sparizione entita in HA e lentezza nello spegnimento uscite reali.
- E' stato anche rilevato rollback del setpoint quando modificato da `climate.e_therm_*`.

## Cause principali identificate
- Reconnect watchdog troppo aggressivo (trigger su `stale_source`).
- Callback MQTT da client stale durante reconnect che potevano sporcare lo stato connessione.
- Publish `offline` retained su disconnessioni transitorie, con impatto negativo su disponibilita entita HA.
- Percorso switch reali con readback sincrono (`GET /states`) prima/dopo ogni comando, fonte di latenza.
- Race setpoint: update sorgente stale sovrascriveva subito il nuovo target climate.

## Fix applicati
- `v2.6.108`
  - reconnect solo su vera disconnessione MQTT (non su stale source).
  - rimosso publish retained `offline` nei drop transitori.
- `v2.6.109`
  - velocizzato ON/OFF switch reali rimuovendo readback bloccante per entita.
- `v2.6.110`
  - anti-rollback setpoint: durante ACK pendente non sovrascrive `TEMP_THR` con valore stale.

## Stato attuale osservato
- MQTT stabile (nessun errore ricorrente riportato negli ultimi test).
- Entita e-Therm non piu scomparse durante hiccup brevi.
- Spegnimento uscite migliorato.
- Setpoint climate non dovrebbe piu tornare subito al valore precedente.

## Parametri PWM discussi (globali)
- Aggressivo consigliato:
  - `pwm_kp: 8`
  - `pwm_ki: 0.08`
  - `pwm_min_to_med: 30`
  - `pwm_med_to_max: 60`
- Molto aggressivo:
  - `pwm_kp: 12`
  - `pwm_ki: 0.15`
  - `pwm_min_to_med: 20`
  - `pwm_med_to_max: 45`

Nota: i parametri PWM in `options` sono globali e valgono per tutti i termostati/profili (incluso `WINE_CELLAR`).

## TODO prossimo step
- Implementare override PWM per-term (per zona), con fallback ai valori globali:
  - campi attesi per termostato: `pwm_kp`, `pwm_ki`, `pwm_windup`, `pwm_min_to_med`, `pwm_med_to_max` (eventualmente anche `pwm_deadband_on/off`).
  - precedenza: valore per-term se presente, altrimenti `options` globali.
  - obiettivo: poter rendere zone come ORANGERIE piu aggressive e zone come WINE_CELLAR piu conservative.
