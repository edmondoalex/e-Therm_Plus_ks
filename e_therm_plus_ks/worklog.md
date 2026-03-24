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

## 2026-01-25  logo updated  Autore: Automator
- Replaced addon logo with www/eTherm addon.png

## 2026-01-25 — 2.0.4 — Autore: Automator
- Bumped addon version to `2.0.4` after admin/UI fixes; updated `UI_REV` and worklog.
- File modificati: `config.yaml`, `app/debug_server.py`, `worklog.md`.

## 2026-01-25 — 2.0.5 — Autore: Automator
- Automated bump to 2.0.5
- File modificati: config.yaml, app/debug_server.py

## 2026-01-25 — 2.0.6 — Autore: Automator
- Bump to 2.0.6 before index_debug test
- File modificati: config.yaml, app/debug_server.py

## 2026-01-25 — 2.0.7 — Autore: Automator
- Bump to 2.0.7; cleaned index_debug and worklog consolidation
- File modificati: config.yaml, app/debug_server.py, worklog.md

## 2026-01-25 — 2.0.8 — Autore: Codex
- index_debug: logo in alto e versione mostrata = versione add-on (da `config.yaml` / `ADDON_VERSION`), non UI rev.
- File modificati: `config.yaml`, `app/debug_server.py`, `worklog.md`.


## 2026-01-25 — 2.0.9 — Autore: Codex
- index_debug: logo in header; mostra versione add-on (non UI)
- File modificati: config.yaml, app/debug_server.py, worklog.md


## 2026-01-25 — 2.0.10 — Autore: Codex
- index_debug: asset path compatibile con Ingress + fallback versione (CODE_VERSION)
- File modificati: config.yaml, app/debug_server.py, scripts/bump_release.py, worklog.md

## 2026-01-26 ? 2.1.0 ? Autore: Codex
- Allineata documentazione/README a e-Therm Plus KS.
- MQTT: comandi `power`/`fan3` funzionanti (subscribe su `.../set`), stato retained e interlock fan3; stato manuale persistito in `/data/vtherm_runtime.json`.
- Web auth: `none`/`basic`/`token` (token via `?token=...` imposta cookie HttpOnly).
- Packaging: Dockerfile copia `config.yaml` in image; `run.sh` non richiede pi? bashio.
- File modificati: README.md, README_ADDON.md, config.yaml, Dockerfile, run.sh, app/main.py, app/debug_server.py, worklog.md


## 2026-01-26 — 2.1.1 — Autore: Codex
- Bump versione dopo fix runtime/UI/auth.
- File modificati: config.yaml, app/debug_server.py, worklog.md


## 2026-01-26 — 2.1.2 — Autore: Codex
- Fix allineamento UI termostato: realtime.THERM + static WIN/SUM; implementati comandi set_season/set_mode/set_profile/set_schedule.
- File modificati: app/main.py, config.yaml, app/debug_server.py, worklog.md


## 2026-01-26 — 2.2.0 — Autore: Codex
- Aggiunto clone bidirezionale MQTT climate (discovery) per termostati e-safe: comandi HA→e-safe (mode/preset/setpoint) e stato e-safe→HA.
- File modificati: app/main.py, config.yaml, app/debug_server.py, worklog.md


## 2026-01-26 — 2.3.0 — Autore: Codex
- Aggiunto controllo automatico PWM (PI) + mapping fan3 (min/med/max) con override manuale; nuove opzioni in config.yaml.
- File modificati: config.yaml, app/main.py, worklog.md


## 2026-01-26 — 2.3.1 — Autore: Codex
- Schema opzioni: esposte in UI auto_control_enabled e parametri PWM/fan3.
- File modificati: config.yaml, worklog.md, app/debug_server.py


## 2026-01-26 — 2.3.2 — Autore: Codex
- Fix schema opzioni: pwm_* come float (risolve invalid options) + descrizione corretta.
- File modificati: config.yaml, worklog.md, app/debug_server.py


## 2026-01-26 — 2.3.3 — Autore: Codex
- Auto cleanup MQTT Discovery: rimuovendo un vTherm da /vtherm l’add-on cancella i topic homeassistant/.../config retained (es. Cantina 2).
- File modificati: app/main.py, config.yaml, app/debug_server.py, worklog.md


## 2026-01-27 — 2.4.0 — Autore: Codex
- UI vTherm user-friendly: CRUD termostati (aggiungi/modifica/duplica/elimina) + salvataggio, con editor JSON avanzato.
- File modificati: app/debug_server.py, config.yaml, worklog.md


## 2026-01-27 — 2.5.0 — Autore: Codex
- Uscite separate per stagione: supporto outputs_heat/outputs_cool (heat vs cool) con topic MQTT e discovery distinti; UI vTherm aggiornata.
- File modificati: app/main.py, app/debug_server.py, config.yaml, worklog.md


## 2026-01-27 — 2.5.1 — Autore: Codex
- Auto control per-termometro: auto_control_enabled configurabile per ogni vTherm (fallback al globale); UI /vtherm aggiornata.
- File modificati: app/main.py, app/debug_server.py, config.yaml, worklog.md


## 2026-01-27 — 2.5.2 — Autore: Codex
- UI /vtherm: aggiunta descrizione accurata (guida configurazione e significato campi).
- File modificati: app/debug_server.py, config.yaml, worklog.md


## 2026-01-27 — 2.5.3 — Autore: Codex
- UI vTherm: aggiunto pulsante Ricarica anche nel box Salvataggio e reso più visibile.
- File modificati: app/debug_server.py


## 2026-01-27 — 2.5.4 — Autore: Codex
- README: spiegazione dettagliata (a prova di bambino) dei parametri default_profile e auto control (PWM/fan).
- File modificati: README.md


## 2026-01-27 — 2.5.5 — Autore: Codex
- Watchdog stabile: auto-reconnect MQTT con backoff + ripartenza control thread; health visibile in /vtherm.
- File modificati: app/main.py, config.yaml, app/debug_server.py


## 2026-01-27 — 2.5.6 — Autore: Codex
- Stabilita: ignora messaggi MQTT retained sui topic di comando */set per evitare override/auto bloccato dopo resubscribe.
- File modificati: app/main.py


## 2026-01-27 — 2.5.7 — Autore: Codex
- Registro eventi e-Therm: log dettagliato con origine (esafe/ui/ha_mqtt/auto/system), persistito su /data/e_therm_events.jsonl; /logs include filtri origine/termostato e live update.
- File modificati: app/main.py, app/debug_server.py


## 2026-01-27 — 2.5.8 — Autore: Codex
- Fix /logs vuoto: gli eventi e-Therm ora vengono pubblicati anche come entita type=logs (SSE live), oltre al salvataggio JSONL.
- File modificati: app/main.py


## 2026-01-27 — 2.5.9 — Autore: Codex
- Diagnostica /logs: evento startup + pulsante Test log; handler e_therm/log_test.
- File modificati: app/main.py, app/debug_server.py


## 2026-01-27 — 2.6.0 — Autore: Codex
- Fix /logs in Ingress: usa apiUrl() per /api/stream e /api/cmd; pulsante test log funziona anche via hassio_ingress.
- File modificati: app/debug_server.py


## 2026-01-28 — 2.6.1 — Autore: Codex
- Fix /logs ancora vuoto: aggiunto polling /api/entities ogni 5s + refresh dopo Test log (fallback se SSE bloccato).
- File modificati: app/debug_server.py


## 2026-01-28 — 2.6.2 — Autore: Codex
- Riduzione log: log_level (MIN/NORMAL/DEBUG), auto PWM throttling (step/time/stage), telemetria solo DEBUG, ACK/timeout per comandi UI/HA.
- File modificati: config.yaml, app/main.py


## 2026-01-28 — 2.6.3 — Autore: Codex
- Logs UI: aggiunta esportazione TXT leggibile (rispetta filtri e ricerca).
- File modificati: app/debug_server.py


## 2026-01-28 — 2.6.4 — Autore: Codex
- Log file cap: aggiunto log_file_max_kb e trimming automatico del file JSONL mantenendo gli eventi piu recenti.
- File modificati: config.yaml, app/main.py


## 2026-01-28 — 2.6.5 — Autore: Codex
- Stabilita UI: /vtherm ora ha try/except e restituisce errore leggibile invece di pagina irraggiungibile.
- File modificati: app/debug_server.py


## 2026-01-28 — 2.6.6 — Autore: Codex
- Fix /vtherm crash: import typing.Any in debug_server.
- File modificati: app/debug_server.py

2026-03-24 | 2.6.7 | codex | Publish valve topic per termostato (PWM/stadi ON/OFF) | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.8 | codex | Aggiorna client MQTT (Callback API v2) | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.9 | codex | Fix callback signature for paho-mqtt v2 | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.10 | codex | Accept extra args in MQTT callbacks for compatibility | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.11 | codex | Log versione all'avvio per debug immagine | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.12 | codex | Force local build (build: true) to apply code updates | e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.13 | codex | Hardening MQTT callback compatibility (avoid v1/v2 mismatch crash) | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
