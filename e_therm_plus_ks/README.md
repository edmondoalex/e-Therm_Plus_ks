# e-Therm Plus KS

Add-on per E-Manager/Home Assistant: crea **termostati virtuali** partendo dai dati pubblicati dall'add-on **e-safe** su MQTT e genera entita dedicate (MQTT Discovery) per uscite manuali (power 0-100, fan 3 stadi).

## Requisiti
- Broker MQTT (es. Mosquitto add-on)
- Add-on e-safe gia attivo (topic `e-safe/thermostats/<num>` con payload JSON)

## Config (opzioni)
- `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`
- `source_prefix` (default `e-safe`)
- `out_prefix` (default `e-therm`)
- `web_auth_mode`: `none` | `basic` | `token`
  - `basic`: `web_basic_user`, `web_basic_pass`
  - `token`: `web_token`
- `default_profile` (default `WINE_CELLAR`)
  - Cos'e: e' il **profilo predefinito** proposto quando crei un nuovo vTherm dalla pagina `/vtherm`.
  - Cosa fa: **non cambia da solo** come lavora il termostato; e' un preset/etichetta comoda per partire piu veloce e mantenere ordine.
  - Valori:
    - `WINE_CELLAR`: cantina (tipicamente temperature piu basse e stabili)
    - `HOUSE_ECO`: casa modalita risparmio
    - `HOUSE_COMFORT`: casa modalita comfort

### Auto control (PWM + fan3) - spiegazione "a prova di bambino"
Queste opzioni servono quando vuoi che e-Therm **comandi da solo** le uscite (PWM e/o Fan3) in base alla temperatura e al setpoint del termostato e-safe.

Nota importante:
- Ogni vTherm ha anche il suo interruttore in `/vtherm` ("Auto control per questo vTherm"). Se lo imposti li, **vale quello**. Se non lo imposti, l'add-on usa questi valori come **default**.

**`auto_control_enabled` (true/false)**
- Cos'e: l'interruttore generale dell'auto-pilot.
- `true` = e-Therm prova a tenere la temperatura "giusta" regolando:
  - `power` (PWM 0-100) e/o
  - `fan3` (MIN/MED/MAX),
  solo se nel vTherm hai abilitato quelle uscite.
- `false` = e-Therm **non comanda automaticamente** PWM/fan. Le entita MQTT Discovery delle uscite possono comunque esistere (se abilitate), ma le comandi **tu** manualmente da Home Assistant.

**`auto_override_sec` (secondi)**
- Cos'e: il "tempo di rispetto" quando tocchi manualmente le uscite.
- A cosa serve: se tu accendi/spegni o cambi la velocita da Home Assistant, e-Therm aspetta questo numero di secondi prima di riprendere l'auto (cosi non "litiga" con te).
- Esempio: `300` = per 5 minuti l'auto resta fermo dopo un tuo comando.

**`pwm_kp`**
- Cos'e: quanto e-Therm reagisce **subito** all'errore di temperatura.
- Tradotto: se sei lontano dal setpoint, con `kp` piu alto "spinge" piu forte (PWM sale piu rapidamente).
- Se e' troppo alto: puo oscillare (su/giu) e diventare nervoso.

**`pwm_ki`**
- Cos'e: quanto e-Therm "impara nel tempo" e corregge gli errori piccoli ma costanti.
- Tradotto: se sei sempre un po' sotto/sopra, `ki` aggiusta lentamente finche ci arrivi.
- Se e' troppo alto: puo accumulare troppo e poi "sparare" (overshoot).

**`pwm_windup`**
- Cos'e: il "tappo" all'accumulo di `ki` (limite anti-esagerazione).
- A cosa serve: impedisce al controllo di accumulare un valore enorme quando per un po' non puo raggiungere il setpoint (es. impianto spento, porte aperte, ecc.).

**`pwm_deadband` (C)**
- Cos'e: la "zona di tranquillita" intorno al setpoint.
- A cosa serve: se sei gia molto vicino al setpoint, e-Therm evita micro-correzioni continue.
- Esempio: `0.2` = se sei entro +/- 0.2C dal setpoint, tende a non fare correzioni aggressive.

**`pwm_min_to_med` (0-100)**
- Cos'e: la soglia che decide quando passare la ventola da **MIN** a **MED**.
- A cosa serve: e-Therm calcola un PWM (0-100) e poi lo trasforma in 3 velocita.
- Esempio: `34` = se PWM e' 34 o piu, la ventola puo andare almeno in MED (se la ventola e' abilitata).

**`pwm_med_to_max` (0-100)**
- Cos'e: la soglia che decide quando passare la ventola da **MED** a **MAX**.
- Esempio: `67` = se PWM e' 67 o piu, la ventola puo andare in MAX.

## UI
- `/menu` launcher
- `/thermostats` lista
- `/vtherm` configurazione (salva su `/data/vtherm.json`)
- `/index_debug` diagnostica

## MQTT (uscita)
- Power: state `e-therm/thermostats/<id>/power`, command `.../power/set`
- Fan: state `e-therm/thermostats/<id>/fan/<min|med|max>`, command `.../fan/<...>/set`

Nota: lo stato manuale e' persistito in `/data/vtherm_runtime.json`.

