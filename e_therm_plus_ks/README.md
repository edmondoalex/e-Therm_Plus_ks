# e-Therm Plus KS

Termostati virtuali manuali con UI identica, PWM e gestione 3-relay (in sviluppo).

## Ingress
- UI disponibile via Ingress su `/menu`.

## Opzioni
- `mqtt_host`: host broker MQTT (default: `core-mosquitto`)
- `mqtt_port`: porta broker MQTT (default: `1883`)
- `mqtt_user`: utente MQTT (opzionale)
- `mqtt_password`: password MQTT (opzionale)
- `source_prefix`: prefisso topic sorgente (default: `e-safe`)
- `out_prefix`: prefisso topic output (default: `e-therm`)

- `web_auth_mode`: `none` | `basic` | `token`
- `web_basic_user`: utente Basic Auth (se `basic`)
- `web_basic_pass`: password Basic Auth (se `basic`)
- `web_token`: token (se `token`)

- `control_interval_sec`: intervallo controllo (secondi)
- `default_profile`: `WINE_CELLAR` | `HOUSE_ECO` | `HOUSE_COMFORT`

## Porte
- `8080/tcp`: UI web