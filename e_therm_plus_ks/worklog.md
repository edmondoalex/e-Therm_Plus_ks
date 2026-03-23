# Worklog — e-Therm_Plus_ks
Questo file traccia in modo minimale tutte le modifiche significative al progetto.

---

2026-01-25 — v2.0.2-1 — Autore: MarioR
- Aggiunto modulo `app/pwm_controller.py` che fornisce una base PI e la mappatura PWM->stadi (MIN/MED/MAX) con interlock logico.
- Modificato `app/main.py` per pubblicare gli stati MQTT iniziali per ogni termostato (`.../thermostats/<id>/power` e `.../fan/<sp>`) dopo il sync realtime, in modo che le entità create tramite MQTT Discovery abbiano uno stato iniziale.
- Eseguiti test locali: simulato un payload JSON e-safe e verificato il parsing in `ThermEngine._on_message`; simulata la pubblicazione discovery e lo stato MQTT.
- File modificati/aggiunti: `app/pwm_controller.py`, `app/main.py`, `worklog.md`, `README_ADDON.md`.

Note operative:
- Il publish dei valori fan/power è minimale (power pubblicato come valore `TEMP_THR.VAL` se presente; fan pubblicati come `OFF` di default). L'integrazione reale con l'attuatore e la logica PWM verrà implementata nello step B.
- Per testare localmente ho installato la dipendenza `paho-mqtt` nella virtualenv del progetto.

Prossimi passi consigliati (breve):
1. Integrare `PWMController` nella logica di controllo (ThermEngine) e salvare i parametri nel config persistente (`/data/vtherm.json`).
2. Aggiornare UI `vtherm` per esporre i controlli PWM e i toggle dei tre relè (solo visualizzazione nella fase A).
3. Preparare simulatore WebSocket per test end-to-end senza centrale reale.

---

2026-01-25 — v2.0.2-2 — Autore: MarioR
- Aggiunto script `scripts/add_thermostat.py` per creare termostati di test in `data/vtherm.json`.
  - Comando d'esempio:
    - `python scripts/add_thermostat.py --id 10 --name "Cantina" --source-num 1 --power --fan3`
  - Lo script scrive `./data/vtherm.json` e prova anche a scrivere `/data/vtherm.json` per compatibilità runtime.

- Aggiungere una voce in questo `worklog.md` con: data ISO — versione — autore — breve descrizione — file modificati.

---

2026-01-25 — v2.0.2 — Autore: MarioR
- Creazione worklog iniziale e README minimale.
- Resoconto consegna completo incluso (vedi sotto).
- File aggiunti/modificati: `worklog.md`, `readme.md`.

---

# Resoconto completo progetto “e-Therm Plus KS” (consegnare a Codex)

1) Obiettivo generale

Vogliamo creare un nuovo add-on Home Assistant (non modificare quello originale e-safe) chiamato:

`e-Therm_Plus_ks`

Scopo: gestire termostati virtuali che prendono setpoint + stato + temperatura dal termostato “e-safe” (Ksenia/Lares) e generano invece uscite evolute per fan-coil/impianti:

- 3 relè interbloccati (MIN/MED/MAX) → mai due attivi insieme
- PWM 0–100% (inizialmente come valore logico; in futuro 0–10V reale)
- supporto a impianti: radiatori, pavimento, fan-coil, pavimento+fan-coil caldo; freddo solo fan-coil

Uso tipico: cantine vini 365gg (temperatura stabile) + seconda casa (eco minima + comfort quando serve).

In questa fase (step A) implementiamo solo:
- ✅ lettura corretta valori da e-safe e visualizzazione in UI identica
- (no PWM e stadi ancora: verrà dopo).

2) Requisiti UI e accesso

- Interfaccia identica alla UI termostato e-safe.
- Accesso Ingress + porta pubblica configurabile + modalità di autenticazione: None / Basic / Token.
- Pagine richieste: menu, termostati, debug/config (vtherm).
- Rimuovere funzioni di centrale sicurezza: rimanere solo termostati.

3) Configurazione / Debug

- Pagina `/vtherm` per creare termostati virtuali, scegliere sorgente (e-safe thermostat #), scegliere funzionalità uscite (fan3, pwm/power, ecc.) e salvare in `/data/vtherm.json`.
- Problemi risolti: POST /api/vtherm/config mancante -> si usa `POST /api/cmd` con payload `{ "type":"vtherm_config","action":"save","value":{...}}`.
- Fix per evitare crash su f-string in template HTML e conversione `str`->`bytes` nelle risposte.

4) MQTT — architettura

- Broker configurabile (host/port/user/pw).
- Discovery Home Assistant: tutte le entità dei vTherm sotto device unico `e-Therm Termostati`.

Entità richieste per ogni vTherm (step B):
- `number` ... power (0–100) → PWM/power logico
- `switch` ... fan_min
- `switch` ... fan_med
- `switch` ... fan_max

(I tre switch fan devono essere interbloccati — implementazione in fase successiva.)

5) Formato reale dei topic e-safe

- Topic reale: `e-safe/thermostats/<num>` con payload JSON unico.
- Mappatura: `TEMP`, `RH`, `THERM` (ACT_SEA, ACT_MODEL, OUT_STATUS), `WIN.TM`, `SUM.TM`, `THERM.TEMP_THR.VAL`.
- Il codice deve sottoscrivere sia `e-safe/thermostats/+` (JSON) sia `e-safe/thermostats/+/+` (compatibilità).

6) Comandi verso e-safe

- Pass-through esistente per:
  - `e-safe/cmd/thermostat/<num>/temperature`
  - `e-safe/cmd/thermostat/<num>/mode`
  - `e-safe/cmd/thermostat/<num>/preset_mode`

7) Problemi tecnici incontrati (storico)

- SyntaxError dovuto a riga corrotta in `app/main.py` (duplicazione `def _publish_discovery`).
- Errori di template / bytes/str nella UI; fix applicati in versione FULL_FIXED.
- Necessario verificare la stabilità della sottoscrizione MQTT e dell'integrazione realtime WS.

8) Step successivi (per Codex)

Step A (consegna):
- installare FULL_FIXED
- verificare UI termostati mostri valori reali
- verificare `vtherm` non crash e salvi config
- verificare MQTT discovery crea device e entità

Step B (funzionalità "wow"):
- implementare PWM 0–100 basato su ΔT, inerzia e profili
- mappare PWM -> stadi fan: 0–33 MIN, 34–66 MED, 67–100 MAX (parametrico)
- implementare interlock robusto degli switch
- migliorare algoritmo di controllo (PI/PID-like)

---

(Fine resoconto iniziale)

## 2026-01-25 A
- Added VTherm admin page and removed security PIN/WS UI items.


## 2026-01-25 — 2.0.3 — Autore: Automator
- Automated test bump
- File modificati: config.yaml, app/debug_server.py


## 2026-01-25 — 2.0.3 — Autore: Automator
- Automated test bump
- File modificati: config.yaml, app/debug_server.py


## 2026-01-25  logo updated  Autore: Automator
- Replaced addon logo with www/eTherm addon.png 

2026-03-23 | 2.0.4 | codex | Aggiorna client MQTT (Callback API v2) e bump versione | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
