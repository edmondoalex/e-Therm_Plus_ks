# Worklog â€” e-Therm_Plus_ks
Questo file traccia in modo minimale tutte le modifiche significative al progetto.

---

2026-06-08 - v2.6.138 - Autore: Codex
- UI dettaglio termostato: valori principali pre-renderizzati lato server nello HTML iniziale.
- Temperatura, setpoint, RH, stato relay e colore anello non aspettano piu il completamento del JavaScript client.
- Ridotto il ritardo percepito all'apertura da elenco/proxy su tablet e browser lenti.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-08 - v2.6.137 - Autore: Codex
- vTherm `ha_sensor`/`ha_multi_sensor_avg`: aggiunto campo UI `Sensore umidita HA` salvato in `source.humidity_entity_id`.
- Runtime: polling del sensore umidita dedicato e aggiornamento live di `RH`, pubblicato poi anche verso MQTT/HA.
- Supportati sensori con valore nello stato e entita con attributi `current_humidity`/`humidity`.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-07 - v2.6.136 - Autore: Codex
- UI elenco termostati: aggiunto refresh automatico su eventi stato tramite SSE con fallback polling ogni 2 secondi.
- Le righe elenco aggiornano in-place icona, classe `heat/cool/off`, pillola stato e meta temperatura/setpoint senza ricaricare pagina.
- Gestiti gli update parziali dello stream ricaricando le entita complete prima del render lista.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-07 - v2.6.135 - Autore: Codex
- UI dettaglio termostato: layout responsive per mobile/tablet con ring dimensionato anche sull'altezza viewport.
- Topbar e badge compattati per evitare sovrapposizione del titolo su smartphone.
- Controlli `Modalita`/`Extra`, testi interni e manopola ridimensionati per non tagliare il cerchio su tablet.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-07 - v2.6.134 - Autore: Codex
- HA climate bridge: sincronizzazione bidirezionale anche di `heat/cool/off` dal climate reale verso il clone e-Therm.
- Se il climate reale viene acceso/spento manualmente, il clone aggiorna `ACT_SEA/ACT_MODEL` dopo la finestra anti-rimbalzo.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-07 - v2.6.133 - Autore: Codex
- HA climate bridge: sincronizzazione bidirezionale del setpoint per catena clone e-Therm -> climate reale.
- Se il setpoint cambia sul climate reale, il clone e-Therm aggiorna `TEMP_THR` dopo una breve finestra anti-rimbalzo.
- Dopo un comando setpoint dal clone, e-Therm evita di reimportare subito valori vecchi dal climate reale.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-07 - v2.6.132 - Autore: Codex
- HA sensor bridge: quando `source.entity_id` e un `climate.*`, la temperatura attuale viene letta da `current_temperature` invece che dal target `temperature`.
- Aggiunti fallback per attributi custom `DISPLAY_TEMPERATURE` / `TEMPERATURE` sui climate HA non standard.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-07 - v2.6.131 - Autore: Codex
- HA climate bridge: se un termostato `ha_sensor` usa direttamente un `climate.*` come `source.entity_id`, quel climate viene usato anche come target reale da comandare.
- Permette configurazioni tipo `source: ha_sensor` + `entity_id: climate.bagno_mansarda` senza dover aggiungere manualmente `real_thermostat.entity_id`.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.130 - Autore: Codex
- UI termostati: aggiunte route dirette proxy-friendly `/t` per elenco e `/t/<id-o-nome>` per dettaglio.
- Il dettaglio accetta ID numerico o nome normalizzato, es. `/t/11` o `/t/bagno-mansarda`.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.129 - Autore: Codex
- HA climate bridge: i comandi del clone e-Therm vengono inoltrati anche con sorgente `ha_sensor`/virtuale quando e configurato `real_thermostat.entity_id`.
- Supporta la catena: altro add-on -> climate clone e-Therm -> e-Therm -> climate HA reale.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.128 - Autore: Codex
- MQTT Discovery: ascolta i retained `homeassistant/climate/+/config` e cancella automaticamente i climate e-Therm orfani non presenti nella config.
- Cleanup termostati rimossi: genera varianti name-based senza prefisso `e-Therm` per cancellare topic tipo `e_therm_z_giorno_mansarda_climate`.
- Memorizza i topic discovery pubblicati per poterli rimuovere esattamente alle cancellazioni future.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.127 - Autore: Codex
- Cancellazione termostati: rimuove subito l'entita dallo snapshot UI, runtime cache, desired state e latch interni.
- Cleanup MQTT piu completo per termostati rimossi: discovery id/name-based, state topic, command topic e valvole retained.
- Recupera e pulisce anche termostati orfani gia spariti dalla config ma ancora presenti nello snapshot UI.
- File modificati: app/main.py, app/debug_server.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.126 - Autore: Codex
- HA climate bridge: blocca anche `preset_mode=OFF` MQTT durante la finestra di hold dopo `heat/cool`.
- Evita che preset OFF ecoati da Home Assistant richiamino internamente `mode=off` e alimentino il loop heat/off.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.125 - Autore: Codex
- HA climate bridge: ignora comandi MQTT `off` transitori durante la finestra di hold dopo `heat/cool`.
- Quando invia `heat/cool` a un climate HA reale, invia subito anche il setpoint corrente per evitare che il climate resti senza target e torni `off`.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.124 - Autore: Codex
- HA climate bridge: aggiunta tenuta ottimistica dello stato `heat/cool` per 30s dopo comando mode.
- Evita che il polling del climate reale, ancora `off` durante la transizione, ripubblichi subito `OFF` sul climate e-Therm.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.123 - Autore: Codex
- HA climate bridge: aumentato cooldown dei comandi `heat/cool` ripetuti sullo stesso climate reale per fermare loop heat/off.
- MQTT startup: pulizia retained anche per command topic climate (`mode/set`, `target_temperature/set`, `preset_mode/set`), non solo valvole.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.122 - Autore: Codex
- HA climate bridge: aggiunto anti-rimbalzo per non reinviare mode/setpoint identici troppo ravvicinati.
- Prima di comandare un climate HA reale, ora controlla stato e target correnti per evitare loop di accensione/spegnimento.
- Il cambio setpoint ora invia prima il modo solo se serve, poi la temperatura; `set_temperature` con `hvac_mode` resta solo fallback.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.121 - Autore: Codex
- HA climate bridge: comandi mode/setpoint resi piu robusti per climate reali spenti (`turn_on`, `set_hvac_mode`, `set_temperature` con `hvac_mode`).
- Migliora controllo di termostati Home Assistant che espongono `temperature: null` quando sono in `off`.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.120 - Autore: Codex
- MQTT Discovery climate: entity_id e nome entita basati sul nome termostato (`climate.e_therm_<nome>`).
- Cleanup discovery delle versioni climate precedenti basate su ID (`v1/v2/v3/v4`) per forzare la migrazione dei vecchi.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.119 - Autore: Codex
- MQTT Discovery climate: aggiunto `default_entity_id` stabile `climate.e_therm_<id>_climate`.
- Discovery climate migrata a `v3` e cleanup automatico dei vecchi topic `v1/v2` per evitare entity_id duplicati basati sul nome stanza.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.118 - Autore: Codex
- UI elenco termostati: sostituita lista semplice con righe cliccabili e indicatore HEAT/COOL/OFF.
- Badge e icona laterale usano gli stessi colori della pagina dettaglio: arancione heat, blu cool, grigio off.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.117 - Autore: Codex
- UI dettaglio termostato: colore richiesta HEAT reso piu arancione su anello e indicatore centrale.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.116 - Autore: Codex
- UI dettaglio termostato: titolo browser cambiato da `Ksenia Lares - <nome>` a `Termostato - <nome>`.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.115 - Autore: Codex
- Persistenza runtime: setpoint, stagione/modalita e preset dei climate virtuali vengono salvati subito in `vtherm_runtime.json`.
- Evita perdita di setpoint/modalita dopo riavvio addon per sorgenti locali come `ha_sensor` e `virtual`.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.114 - Autore: Codex
- UI dettaglio termostato: aggiunto indicatore centrale relè/richiesta sotto RH.
- Indicatore giallo `HEAT ON` in richiesta caldo, blu `COOL ON` in richiesta freddo, grigio `OFF` senza richiesta.
- File modificati: app/debug_server.py, app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.113 - Autore: Codex
- Fix MQTT Discovery dopo salvataggio `/vtherm`: il discovery viene ripubblicato subito dopo la config save.
- Evita che Home Assistant perda temporaneamente le entita MQTT dopo cleanup/modifica di un termostato.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-06-06 - v2.6.112 - Autore: Codex
- Aggiunta sorgente `ha_sensor` per creare vTherm da una sonda temperatura Home Assistant.
- Aggiunta sorgente `virtual` locale per esporre un climate MQTT senza sorgenti esterne.
- UI `/vtherm`: aggiunti source `ha_sensor`/`virtual` e campi relè Heat/Cool (`switch.*`).
- Backend: mapping `power_switch` per pilotare relè reali separati su heat/cool con uscite split.
- File modificati: app/main.py, app/debug_server.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-29 - v2.6.111 - Autore: Codex
- Registro Eventi: aggiunti in header `File KB` e `Eventi su disco` (metadati runtime da `e_therm_events_meta`).
- Backend log: aggiornati metadati dimensione/conteggio file eventi con refresh dopo trim e aggiornamento incrementale a runtime.
- File modificati: app/main.py, app/debug_server.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-29 - v2.6.110 - Autore: Codex
- Fix setpoint climate clone: evitato rollback immediato al valore vecchio quando arriva un update e-safe stale dopo `target_temperature/set`.
- Durante ACK pendente del setpoint, `TEMP_THR` non viene sovrascritto da valori sorgente diversi dall'atteso.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-29 - v2.6.109 - Autore: Codex
- Migliorata reattivita spegnimento switch reali: rimosso doppio readback sincrono HA (`GET /states`) per ogni comando `switch.turn_on/off`.
- Applicato fast-path su cache comando riuscito per ridurre latenza quando molte entita devono spegnersi insieme.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-29 - v2.6.108 - Autore: Codex
- Stabilita MQTT/HA: rimosso reconnect forzato su `stale_source` (si riconnette solo su vera disconnessione MQTT).
- Evitato publish retained `offline` su disconnect transitori per non far sparire/andare unavailable le entita e-Therm in HA.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-29 - v2.6.107 - Autore: Codex
- Fix MQTT: ripristinato filtro callback per client stale su `on_connect`/`on_disconnect` (evita falsi `mqtt_not_connected` durante reconnect).
- Reintrodotta opzione `watchdog_reconnect_on_stale_source` (default `false`) per disattivare reconnect periodici su sorgente non aggiornata.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.106 - Autore: Codex
- Rollback mirato della gestione reconnect MQTT al comportamento stabile pre-regressione (baseline 2.6.87).
- Ridotto burst MQTT su reconnessione: republish discovery completo limitato a massimo 1 volta ogni 300s.
- Rimossa opzione non piu usata `watchdog_reconnect_on_stale_source` per coerenza config/runtime.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.104 - Autore: Codex
- Risolto reconnect periodico non desiderato: il watchdog non forza piu reconnect su `stale_source` di default.
- Nuova opzione `watchdog_reconnect_on_stale_source` (default `false`) per riattivare il comportamento precedente solo se richiesto.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.103 - Autore: Codex
- Aggiunto marker di stabilità MQTT: log `[INFO] MQTT stable: connected for >=60s` dopo 60s continuativi connessi.
- Migliorata diagnostica runtime per distinguere chiaramente un reconnect iniziale da instabilità reale.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.102 - Autore: Codex
- Fix ulteriore reconnect MQTT: introdotto stato `connect in progress` con finestra di guardia (20s) per evitare reconnect multipli durante handshake/callback.
- Evita churn di client (`connected`/`closed` a raffica) quando il broker risponde ma il watchdog interviene troppo presto.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.101 - Autore: Codex
- Fix reconnect storm MQTT: ignorati callback `on_connect`/`on_disconnect` provenienti da client stale sostituiti durante reconnect watchdog.
- Evita falsi `mqtt_not_connected` causati da disconnect tardivi del client precedente.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.100 - Autore: Codex
- Ripristinati i comandi manuali valvole MQTT anche con `auto_control_enabled` attivo.
- Manteniamo il filtro sui soli comandi retained (`.../set` con retain) per evitare restore indesiderato al riavvio.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.99 - Autore: Codex
- Blocco comandi manuali valvole via MQTT quando `auto_control_enabled` è attivo (evita riaccensioni `valv/valv_hot/valv_low` da restore stato HA dopo reboot).
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.97 - Autore: Codex
- Valvole termostato allineate alla logica consenso gruppi: `_calc_auto_valves` ora usa `_consensus_demand_for_therm` (non fallback `OUT_STATUS`).
- In stagione non determinata (`ACT_SEA` non `WIN`/`SUM`) applicato fail-safe OFF per evitare accensione indesiderata della bassa.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.95 - Autore: Codex
- Isteresi centrata sul setpoint (banda completa): COOL ON a set+db e OFF a set-db; HEAT speculare.
- Confermato comportamento richiesto: con set 12.9 e db 0.2 in COOL accende a 13.1 e spegne a 12.7.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---


2026-04-28 - v2.6.94 - Autore: Codex
- Rilascio tuning isteresi: soglie esplicite pwm_deadband_on/off e configurazione simmetrica a 0.2/0.2.
- Allineata versione addon/runtime/README.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---


2026-04-28 - v2.6.93 - Autore: Codex
- Bump versione addon a 2.6.93 per nuovo rilascio.
- Allineata versione runtime mostrata a boot (APP_VERSION) e README addon.
- File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-27 - v2.6.91 - Autore: Codex
- Prevenzione conflitti ciclici sui relÃ¨ reali: i `consensus_groups` non comandano switch giÃ  riservati ai `real_targets` dei termostati (fan/valvole).
- Aggiunto warning diagnostico una tantum: `consensus skip reserved thermostat switch: ...`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-27 - v2.6.90 - Autore: Codex
- Modalita fan reale `strict mirror` (1:1 virtuale->reale): aggiunto `real_fan_strict_mirror` (default `true`) e `real_fan_min_hold_sec` portato a default `0`.
- Diagnostica MQTT: gestione `on_connect rc!=0` con `last_mqtt_error` in health per debug reconnect watchdog.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-27 - v2.6.89 - Autore: Codex
- Fix lampeggio relÃ¨ fan reali: deduplica comandi per entitÃ  fisica in `_apply_real_outputs` (un solo comando finale ON/OFF per ciclo).
- Evitato conflitto ON->OFF nello stesso giro quando uno switch Ã¨ mappato su piÃ¹ bucket velocitÃ  (`min/med/max`).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-27 - v2.6.88 - Autore: Codex
- Stabilizzazione relÃ¨ reali velocitÃ  fan: aggiunto parametro `real_fan_min_hold_sec` (default 20s) per evitare commutazioni rapide ON/OFF tra stadi.
- Applicazione su `real_targets.fan_switches` con memoria stadio effettivo per termostato/stagione.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-27 - v2.6.87 - Autore: Codex
- Fix real switch `CELLAR 1`: evitato effetto ON/OFF immediato quando stagione attiva e inattiva condividono le stesse entita in `real_targets`.
- Aggiunta rilevazione overlap entita reali tra `outputs_heat` e `outputs_cool`; in caso di overlap non viene applicato OFF reale della stagione inattiva.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.86 - Autore: Codex
- Cleanup discovery completo da UI (`type:mqtt/action:cleanup_discovery`) ora supportato lato backend.
- Aggiunto republish discovery da UI (`type:mqtt/action:republish_discovery`).
- Esteso elenco topic di cleanup per includere climate `v1` e `v2` (`e_therm_<id>_climate` e `e_therm_<id>_climate_v2`).
- Fix residui storici in HA: purge discovery legacy su range termostati (1..128) per rimuovere device vecchi cancellati.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.85 - Autore: Codex
- Fix entity_id HA che tornavano a vecchi alias stanza (`lavanderia`, `cabina_armadi`) sui climate virtuali.
- Discovery climate migrata a `unique_id` v2 + `object_id` stabile per sganciarsi dall'entity registry legacy.
- Cleanup automatico del topic discovery legacy `e_therm_<id>_climate`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.84 - Autore: Codex
- OFF bridge reale rinforzato: su domanda OFF invia `climate.turn_off` + `climate.set_hvac_mode(off)` e considera riuscito solo con stato HA realmente `off`.
- Retry OFF automatico nei cicli successivi se HA resta acceso.
- Debug UI esteso: `Stato reale HVAC (HA)`, `Esito cmd OFF`, `Bridge error`.
- Diagnostica eccezioni bridge: reason `BRIDGE_EXCEPTION` con dettaglio `BRIDGE_ERROR`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.83 - Autore: Codex
- Fix spegnimento reale: su `Demand OFF` il bridge verifica lo stato HVAC reale e, se non e `off`, ritenta il comando nei cicli successivi (niente falso `OFF` in cache).
- Aggiunta helper `_ha_climate_state()` per check consistente stato climate.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.82 - Autore: Codex
- Fix comando VMC: accettate anche entita `light.*` e `switch.*` oltre a `fan.*` in `real_thermostat.vmc_entity_id`.
- Per `light.*` usa `light.turn_on` con `brightness_pct` (fallback `turn_on` semplice); per `switch.*` usa `turn_on/turn_off`.
- UI aggiornata nei label VMC per indicare chiaramente supporto `fan/light/switch`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.81 - Autore: Codex
- Aggiunto comando VMC per termostato virtuale: quando c'e richiesta ON (heat/cool) il bridge imposta `fan.<vmc>` a velocita fissa (`vmc_speed_pct`), e a richiesta OFF la spegne (configurabile).
- Nuovi campi UI (vTherm): `Entita VMC (fan)`, `Velocita % (ON)`, `Spegni VMC quando non c'e richiesta`.
- Nuove chiavi config in `real_thermostat`: `vmc_entity_id`, `vmc_speed_pct`, `vmc_off_on_no_demand`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.80 - Autore: Codex
- Spegnimento reale su `ha_multi_sensor_avg` reso immediato a `Demand OFF` (ignora `min_cycle_sec` in OFF per questo source).
- Hardened OFF command: dopo `turn_off`/`set_hvac_mode off` viene verificato lo stato reale HA; se non e `off` non viene considerato riuscito.
- Aggiunta reason diagnostica `MIN_CYCLE_HOLD_OFF` quando lo spegnimento e trattenuto dal ciclo minimo (sorgenti non ha_multi_sensor_avg).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.79 - Autore: Codex
- Fix regressione UI termostato virtuale: corretto errore JS nel render anello (`modeDisp` usato prima della definizione) che bloccava la pagina.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.78 - Autore: Codex
- UI termostato virtuale: colore anello ora guidato dalla richiesta (`DEMAND_ON`) invece che dal solo stato stagione/uscita.
- Regola colore: giallo in richiesta heat, blu in richiesta cool, grigio senza richiesta o in `OFF`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.77 - Autore: Codex
- Fix OFF virtuale: l'azione UI `set_mode=OFF` ora viene mappata a `mode=off` (non piu a `preset_mode`), cosi il termostato reale riceve lo spegnimento HVAC.
- Allineato stato runtime su comando mode: `heat/cool` imposta `ACT_MODEL=MAN`, `off` imposta `ACT_MODEL=OFF` e `ACT_SEA=OFF`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.76 - Autore: Codex
- UI termostato virtuale: rimossi pulsanti laterali `Preset` e `Scheduler`.
- La voce `Modalita` ora include `Inverno`, `Estate` e `Off`; selezione `Off` invia `set_mode=OFF` e spegne virtuale+reale.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.75 - Autore: Codex
- Fix diagnostica bridge reale: inizializzazione default di `DEMAND_ON/DEMAND_REASON` nel polling media sonde e reason esplicito `AUTO_DISABLED` quando il loop salta il controllo automatico.
- In questo modo i campi debug non restano piu vuoti (`-`) e indicano sempre il motivo operativo.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.74 - Autore: Codex
- Aggiunto reason tracing del demand nel controllo virtuale (`THERM.DEMAND_REASON`) e default espliciti per `DEMAND_ON` quando il loop non puo calcolare (manual override, no setpoint, no temp, ecc.).
- Estesa UI Extra con campo `Reason` per capire in tempo reale perche il reale non viene pilotato.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.73 - Autore: Codex
- Aggiunta diagnostica UI in Extra (solo sorgenti media sonde): `Demand (virtuale)`, `Target adattivo calcolato`, `Target reale letto (HA)`.
- Esposti lato runtime i campi `THERM.DEMAND_ON`, `THERM.ADAPT_TARGET`, `THERM.REAL_TARGET_READ` per rendere tracciabile il comando al termostato reale.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.72 - Autore: Codex
- Fix root-cause bridge HA multi-sensor: il polling del termostato reale non sovrascrive piu lo stato di controllo del virtuale (`TEMP_THR`, `ACT_SEA`, `ACT_MODEL`).
- Il polling del reale salva solo telemetria separata (`REAL_TEMP`, `REAL_TARGET`, `REAL_HVAC`, `REAL_HVAC_ACTION`), evitando che il target virtuale venga annullato.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.71 - Autore: Codex
- Reso obbligatorio il bridge adattivo per sorgenti `ha_multi_sensor_avg`: non puo piu essere disattivato da config/UI per errore.
- Disattivato in modo definitivo il sync setpoint diretto verso il termostato reale quando la sorgente e a media sonde (evita overwrite del target adattivo).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.70 - Autore: Codex
- Hardened adaptive setpoint write verso termostato reale: dopo ogni `set_temperature` viene verificato il target letto da HA.
- Se il target non risulta applicato, il motore esegue fallback multipli (refresh hvac_mode, service generico climate, tentativo a step intero).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.69 - Autore: Codex
- Rafforzata la logica adattiva del termostato reale: con domanda ON viene garantito un gap minimo rispetto alla temperatura reale (`+1.0C` in heat, `-1.0C` in cool) per evitare target uguale alla temperatura ambiente.
- Aggiunti parametri opzionali `real_thermostat.demand_min_gap_heat` e `real_thermostat.demand_min_gap_cool` (default 1.0).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.68 - Autore: Codex
- Corretto trigger domanda per termostato reale: in bridge `ha_multi_sensor_avg` la richiesta ON/OFF usa prima l'errore termico (`setpoint` vs `temperatura media`) e non solo il PWM.
- Rafforzato invio `set_temperature` al clima reale con fallback multipli (refresh hvac_mode, path climate generico, tentativo a step intero) per integrazioni piÃ¹ rigide.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.67 - Autore: Codex
- Corretto il clone comandi del termostato virtuale su source `ha_multi_sensor_avg`: quando `adaptive_demand_setpoint` e attivo non viene piu inviato al termostato reale il setpoint del virtuale.
- Evitato il conflitto tra `sync_setpoint` e logica adattiva (target reale derivato da temperatura reale +/- delta), cosi il reale segue davvero la richiesta del virtuale.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.66 - Autore: Codex
- Corretto controllo termostato reale: `min_cycle_sec` non blocca piÃ¹ la transizione a richiesta ON (quindi il setpoint adattivo `+1` in HEAT parte subito).
- `min_cycle_sec` resta applicato solo alla transizione OFF, per evitare cicli troppo rapidi senza ritardare la chiamata calore.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-28 - v2.6.92 - Autore: Codex
- Consenso gruppi PDC allineato alla richiesta reale del termostato: introdotta _consensus_demand_for_therm e uso nei calcoli consenso (pdc e pdc/groups).
- Il consenso non resta piu ON per fallback OUT_STATUS stantio: priorita a DEMAND_ON, fallback solo su output desiderati (power/fan).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---
2026-04-17 - v2.6.65 - Autore: Codex
- UI `/vtherm`: per source `ha_multi_sensor_avg` il form applica preset coerenti (min_cycle default `0`, uscite `power/fan3` disattivate, split OFF).
- I checkbox uscite vengono disabilitati automaticamente per `ha_multi_sensor_avg` e riabilitati sugli altri source.
- Salvataggio robusto: per `ha_multi_sensor_avg` le uscite vengono forzate OFF lato payload, evitando configurazioni incoerenti.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.64 - Autore: Codex
- UI `/vtherm`: aggiunti nel popup i campi per tuning della logica adattiva del termostato reale (`adaptive_demand_setpoint`, delta base/step/max, keepalive, limiti target heat/cool).
- Caricamento/modifica/default/salvataggio dei nuovi parametri direttamente dal form (non solo JSON avanzato).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---
2026-04-17 - v2.6.63 - Autore: Codex
- Termostato reale: aggiunta logica adattiva del setpoint in base alla sua temperatura ambiente quando il virtuale richiede caldo/freddo.
- Supportati delta progressivi con step temporale, limiti min/max target, keepalive comando e reset delta a richiesta OFF.
- Per sorgente `ha_multi_sensor_avg` il comportamento adattivo Ã¨ attivo di default (configurabile nei campi `real_thermostat.*`).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.62 - Autore: Codex
- Pagina dettaglio termostato: per sorgente `ha_multi_sensor_avg` la sezione Extra mostra le temperature delle sonde usate per la media e la media calcolata.
- Nella stessa vista, i profili `T1/T2/T3/TM` vengono nascosti per i termostati a media sonde.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.61 - Autore: Codex
- UI `/vtherm` aggiornata per configurare via form il tipo sorgente `ha_multi_sensor_avg`.
- Aggiunti campi UI: lista sonde HA, `min_valid_sensors`, `stale_sec`, termostato reale `climate`, flag sync e `min_cycle_sec`.
- Estesa serializzazione/sanitizzazione JSON della UI con `real_thermostat` e validazioni dedicate.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-04-17 - v2.6.60 - Autore: Codex
- Nuova sorgente virtuale `ha_multi_sensor_avg`: media di 3 sonde HA per la temperatura misurata del vTherm.
- Aggiunto bridge verso termostato reale (`climate`): sync setpoint/modalita e comando ON/OFF in base alla richiesta del virtuale.
- Aggiunti parametri configurabili: `source.sensors`, `source.min_valid_sensors`, `source.stale_sec`, `real_thermostat.entity_id`, flag di sync e `min_cycle_sec`.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-03-25 ? v2.6.50 ? Autore: Codex
- Consenso gruppi: in HEAT attiva sia gruppo heat che gruppo cool; in COOL attiva solo cool.
- Allineata versione add-on.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

---

2026-01-25 â€” v2.0.2-1 â€” Autore: MarioR
- Aggiunto modulo `app/pwm_controller.py` che fornisce una base PI e la mappatura PWM->stadi (MIN/MED/MAX) con interlock logico.
- Modificato `app/main.py` per pubblicare gli stati MQTT iniziali per ogni termostato (`.../thermostats/<id>/power` e `.../fan/<sp>`) dopo il sync realtime, in modo che le entitÃ  create tramite MQTT Discovery abbiano uno stato iniziale.
- Eseguiti test locali: simulato un payload JSON e-safe e verificato il parsing in `ThermEngine._on_message`; simulata la pubblicazione discovery e lo stato MQTT.
- File modificati/aggiunti: `app/pwm_controller.py`, `app/main.py`, `worklog.md`, `README_ADDON.md`.

Note operative:
- Il publish dei valori fan/power Ã¨ minimale (power pubblicato come valore `TEMP_THR.VAL` se presente; fan pubblicati come `OFF` di default). L'integrazione reale con l'attuatore e la logica PWM verrÃ  implementata nello step B.
- Per testare localmente ho installato la dipendenza `paho-mqtt` nella virtualenv del progetto.

Prossimi passi consigliati (breve):
1. Integrare `PWMController` nella logica di controllo (ThermEngine) e salvare i parametri nel config persistente (`/data/vtherm.json`).
2. Aggiornare UI `vtherm` per esporre i controlli PWM e i toggle dei tre relÃ¨ (solo visualizzazione nella fase A).
3. Preparare simulatore WebSocket per test end-to-end senza centrale reale.

---

2026-01-25 â€” v2.0.2-2 â€” Autore: MarioR
- Aggiunto script `scripts/add_thermostat.py` per creare termostati di test in `data/vtherm.json`.
  - Comando d'esempio:
    - `python scripts/add_thermostat.py --id 10 --name "Cantina" --source-num 1 --power --fan3`
  - Lo script scrive `./data/vtherm.json` e prova anche a scrivere `/data/vtherm.json` per compatibilitÃ  runtime.

- Aggiungere una voce in questo `worklog.md` con: data ISO â€” versione â€” autore â€” breve descrizione â€” file modificati.

---

2026-01-25 â€” v2.0.2 â€” Autore: MarioR
- Creazione worklog iniziale e README minimale.
- Resoconto consegna completo incluso (vedi sotto).
- File aggiunti/modificati: `worklog.md`, `readme.md`.

---

# Resoconto completo progetto â€œe-Therm Plus KSâ€ (consegnare a Codex)

1) Obiettivo generale

Vogliamo creare un nuovo add-on Home Assistant (non modificare quello originale e-safe) chiamato:

`e-Therm_Plus_ks`

Scopo: gestire termostati virtuali che prendono setpoint + stato + temperatura dal termostato â€œe-safeâ€ (Ksenia/Lares) e generano invece uscite evolute per fan-coil/impianti:

- 3 relÃ¨ interbloccati (MIN/MED/MAX) â†’ mai due attivi insieme
- PWM 0â€“100% (inizialmente come valore logico; in futuro 0â€“10V reale)
- supporto a impianti: radiatori, pavimento, fan-coil, pavimento+fan-coil caldo; freddo solo fan-coil

Uso tipico: cantine vini 365gg (temperatura stabile) + seconda casa (eco minima + comfort quando serve).

In questa fase (step A) implementiamo solo:
- âœ… lettura corretta valori da e-safe e visualizzazione in UI identica
- (no PWM e stadi ancora: verrÃ  dopo).

2) Requisiti UI e accesso

- Interfaccia identica alla UI termostato e-safe.
- Accesso Ingress + porta pubblica configurabile + modalitÃ  di autenticazione: None / Basic / Token.
- Pagine richieste: menu, termostati, debug/config (vtherm).
- Rimuovere funzioni di centrale sicurezza: rimanere solo termostati.

3) Configurazione / Debug

- Pagina `/vtherm` per creare termostati virtuali, scegliere sorgente (e-safe thermostat #), scegliere funzionalitÃ  uscite (fan3, pwm/power, ecc.) e salvare in `/data/vtherm.json`.
- Problemi risolti: POST /api/vtherm/config mancante -> si usa `POST /api/cmd` con payload `{ "type":"vtherm_config","action":"save","value":{...}}`.
- Fix per evitare crash su f-string in template HTML e conversione `str`->`bytes` nelle risposte.

4) MQTT â€” architettura

- Broker configurabile (host/port/user/pw).
- Discovery Home Assistant: tutte le entitÃ  dei vTherm sotto device unico `e-Therm Termostati`.

EntitÃ  richieste per ogni vTherm (step B):
- `number` ... power (0â€“100) â†’ PWM/power logico
- `switch` ... fan_min
- `switch` ... fan_med
- `switch` ... fan_max

(I tre switch fan devono essere interbloccati â€” implementazione in fase successiva.)

5) Formato reale dei topic e-safe

- Topic reale: `e-safe/thermostats/<num>` con payload JSON unico.
- Mappatura: `TEMP`, `RH`, `THERM` (ACT_SEA, ACT_MODEL, OUT_STATUS), `WIN.TM`, `SUM.TM`, `THERM.TEMP_THR.VAL`.
- Il codice deve sottoscrivere sia `e-safe/thermostats/+` (JSON) sia `e-safe/thermostats/+/+` (compatibilitÃ ).

6) Comandi verso e-safe

- Pass-through esistente per:
  - `e-safe/cmd/thermostat/<num>/temperature`
  - `e-safe/cmd/thermostat/<num>/mode`
  - `e-safe/cmd/thermostat/<num>/preset_mode`

7) Problemi tecnici incontrati (storico)

- SyntaxError dovuto a riga corrotta in `app/main.py` (duplicazione `def _publish_discovery`).
- Errori di template / bytes/str nella UI; fix applicati in versione FULL_FIXED.
- Necessario verificare la stabilitÃ  della sottoscrizione MQTT e dell'integrazione realtime WS.

8) Step successivi (per Codex)

Step A (consegna):
- installare FULL_FIXED
- verificare UI termostati mostri valori reali
- verificare `vtherm` non crash e salvi config
- verificare MQTT discovery crea device e entitÃ 

Step B (funzionalitÃ  "wow"):
- implementare PWM 0â€“100 basato su Î”T, inerzia e profili
- mappare PWM -> stadi fan: 0â€“33 MIN, 34â€“66 MED, 67â€“100 MAX (parametrico)
- implementare interlock robusto degli switch
- migliorare algoritmo di controllo (PI/PID-like)

---

(Fine resoconto iniziale)

## 2026-01-25 A
- Added VTherm admin page and removed security PIN/WS UI items.

## 2026-01-25 â€” 2.0.3 â€” Autore: Automator
- Automated test bump
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

## 2026-01-25  logo updated  Autore: Automator
- Replaced addon logo with www/eTherm addon.png

## 2026-01-25 â€” 2.0.4 â€” Autore: Automator
- Bumped addon version to `2.0.4` after admin/UI fixes; updated `UI_REV` and worklog.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

## 2026-01-25 â€” 2.0.5 â€” Autore: Automator
- Automated bump to 2.0.5
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

## 2026-01-25 â€” 2.0.6 â€” Autore: Automator
- Bump to 2.0.6 before index_debug test
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

## 2026-01-25 â€” 2.0.7 â€” Autore: Automator
- Bump to 2.0.7; cleaned index_debug and worklog consolidation
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

## 2026-01-25 â€” 2.0.8 â€” Autore: Codex
- index_debug: logo in alto e versione mostrata = versione add-on (da `config.yaml` / `ADDON_VERSION`), non UI rev.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-25 â€” 2.0.9 â€” Autore: Codex
- index_debug: logo in header; mostra versione add-on (non UI)
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-25 â€” 2.0.10 â€” Autore: Codex
- index_debug: asset path compatibile con Ingress + fallback versione (CODE_VERSION)
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

## 2026-01-26 ? 2.1.0 ? Autore: Codex
- Allineata documentazione/README a e-Therm Plus KS.
- MQTT: comandi `power`/`fan3` funzionanti (subscribe su `.../set`), stato retained e interlock fan3; stato manuale persistito in `/data/vtherm_runtime.json`.
- Web auth: `none`/`basic`/`token` (token via `?token=...` imposta cookie HttpOnly).
- Packaging: Dockerfile copia `config.yaml` in image; `run.sh` non richiede pi? bashio.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-26 â€” 2.1.1 â€” Autore: Codex
- Bump versione dopo fix runtime/UI/auth.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-26 â€” 2.1.2 â€” Autore: Codex
- Fix allineamento UI termostato: realtime.THERM + static WIN/SUM; implementati comandi set_season/set_mode/set_profile/set_schedule.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-26 â€” 2.2.0 â€” Autore: Codex
- Aggiunto clone bidirezionale MQTT climate (discovery) per termostati e-safe: comandi HAâ†’e-safe (mode/preset/setpoint) e stato e-safeâ†’HA.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-26 â€” 2.3.0 â€” Autore: Codex
- Aggiunto controllo automatico PWM (PI) + mapping fan3 (min/med/max) con override manuale; nuove opzioni in config.yaml.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-26 â€” 2.3.1 â€” Autore: Codex
- Schema opzioni: esposte in UI auto_control_enabled e parametri PWM/fan3.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-26 â€” 2.3.2 â€” Autore: Codex
- Fix schema opzioni: pwm_* come float (risolve invalid options) + descrizione corretta.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-26 â€” 2.3.3 â€” Autore: Codex
- Auto cleanup MQTT Discovery: rimuovendo un vTherm da /vtherm lâ€™add-on cancella i topic homeassistant/.../config retained (es. Cantina 2).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.4.0 â€” Autore: Codex
- UI vTherm user-friendly: CRUD termostati (aggiungi/modifica/duplica/elimina) + salvataggio, con editor JSON avanzato.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.0 â€” Autore: Codex
- Uscite separate per stagione: supporto outputs_heat/outputs_cool (heat vs cool) con topic MQTT e discovery distinti; UI vTherm aggiornata.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.1 â€” Autore: Codex
- Auto control per-termometro: auto_control_enabled configurabile per ogni vTherm (fallback al globale); UI /vtherm aggiornata.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.2 â€” Autore: Codex
- UI /vtherm: aggiunta descrizione accurata (guida configurazione e significato campi).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.3 â€” Autore: Codex
- UI vTherm: aggiunto pulsante Ricarica anche nel box Salvataggio e reso piÃ¹ visibile.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.4 â€” Autore: Codex
- README: spiegazione dettagliata (a prova di bambino) dei parametri default_profile e auto control (PWM/fan).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.5 â€” Autore: Codex
- Watchdog stabile: auto-reconnect MQTT con backoff + ripartenza control thread; health visibile in /vtherm.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.6 â€” Autore: Codex
- Stabilita: ignora messaggi MQTT retained sui topic di comando */set per evitare override/auto bloccato dopo resubscribe.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.7 â€” Autore: Codex
- Registro eventi e-Therm: log dettagliato con origine (esafe/ui/ha_mqtt/auto/system), persistito su /data/e_therm_events.jsonl; /logs include filtri origine/termostato e live update.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.8 â€” Autore: Codex
- Fix /logs vuoto: gli eventi e-Therm ora vengono pubblicati anche come entita type=logs (SSE live), oltre al salvataggio JSONL.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.5.9 â€” Autore: Codex
- Diagnostica /logs: evento startup + pulsante Test log; handler e_therm/log_test.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-27 â€” 2.6.0 â€” Autore: Codex
- Fix /logs in Ingress: usa apiUrl() per /api/stream e /api/cmd; pulsante test log funziona anche via hassio_ingress.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-28 â€” 2.6.1 â€” Autore: Codex
- Fix /logs ancora vuoto: aggiunto polling /api/entities ogni 5s + refresh dopo Test log (fallback se SSE bloccato).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-28 â€” 2.6.2 â€” Autore: Codex
- Riduzione log: log_level (MIN/NORMAL/DEBUG), auto PWM throttling (step/time/stage), telemetria solo DEBUG, ACK/timeout per comandi UI/HA.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-28 â€” 2.6.3 â€” Autore: Codex
- Logs UI: aggiunta esportazione TXT leggibile (rispetta filtri e ricerca).
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-28 â€” 2.6.4 â€” Autore: Codex
- Log file cap: aggiunto log_file_max_kb e trimming automatico del file JSONL mantenendo gli eventi piu recenti.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-28 â€” 2.6.5 â€” Autore: Codex
- Stabilita UI: /vtherm ora ha try/except e restituisce errore leggibile invece di pagina irraggiungibile.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.


## 2026-01-28 â€” 2.6.6 â€” Autore: Codex
- Fix /vtherm crash: import typing.Any in debug_server.
- - File modificati: app/main.py, config.yaml, README_ADDON.md, worklog.md.

2026-03-24 | 2.6.7 | codex | Publish valve topic per termostato (PWM/stadi ON/OFF) | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.8 | codex | Aggiorna client MQTT (Callback API v2) | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.9 | codex | Fix callback signature for paho-mqtt v2 | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.10 | codex | Accept extra args in MQTT callbacks for compatibility | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.11 | codex | Log versione all'avvio per debug immagine | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.12 | codex | Force local build (build: true) to apply code updates | e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.13 | codex | Hardening MQTT callback compatibility (avoid v1/v2 mismatch crash) | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.14 | codex | Release bump for clean redeploy target | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.15 | codex | Definitive MQTT callback hardening using *args to avoid v1/v2 signature mismatch | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.16 | codex | Add explicit boot marker in logs to verify deployed code version | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.17 | codex | Add robust MQTT callback dispatchers with v1/v2 signature fallback | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.18 | codex | Reconnect now reads live MQTT host/port from options.json to avoid stale core-mosquitto fallback | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.19 | codex | Add diagnostics for options.json read and mqtt_host fallback source | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.20 | codex | Persist and restore realtime e-safe thermostat state on restart to align UI without manual setpoint change | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.21 | codex | Force MQTT callback API v1 compatibility and suppress related deprecation warning in logs | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.22 | codex | Add simple valve topic by thermostat id (e-therm/valv/<id>/set) for easier MQTT filtering | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.23 | codex | Add MQTT Discovery switch valv per thermostat device | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.24 | codex | Fix MQTT switch discovery for valve by adding command_topic | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.25 | codex | Fix valve flapping with split heat/cool by computing valv across both seasons | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.27 | codex | Add general PDC consent switch and dedicated device group name 'e-therm PDC' | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.28 | codex | Recompute/publish PDC consensus on every valve state publish to avoid stale/off delay | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.29 | codex | Add separate PDC consensus states/switches for heat and cool while keeping single valve logic | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.30 | codex | Add ha_climate source support (poll state + send climate service commands) while keeping legacy e-safe thermostats unchanged | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.31 | codex | Enable vTherm UI Add/Edit for ha_climate source with entity_id field and validation | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.32 | codex | Fix ha_climate runtime sync by enabling HA/Supervisor API access and polling in control loop; add explicit HA API diagnostics | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-24 | 2.6.33 | codex | Republish release to force Supervisor refresh and align displayed/addon code versions | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.34 | codex | Split PDC consensus by source group: legacy/e-safe remains on pdc/* and HA-climate thermostats publish to pdc/ha/* with dedicated MQTT discovery switches | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.35 | codex | Add per-thermostat persistent consensus_group mapping in vTherm UI and publish/discovery of dynamic group consensus switches (general/heat/cool) | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.36 | codex | Add persistent per-thermostat real_targets mapping (UI JSON field) and HA service driving for real switch/light outputs; PWM dimmer uses exact 0..100 brightness_pct | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.37 | codex | Replace raw JSON real_targets input with explicit Add/Edit fields (PWM light, valve switch, fan min/med/max switches) while keeping persistent mapping | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.38 | codex | Add UI for consensus group real switch mappings and apply HA switch control for group consensus (general/heat/cool) | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.39 | codex | Auto-populate consensus group UI from existing thermostat consensus_group values | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.40 | codex | Allow creating consensus groups without real switches (optional mapping) | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.41 | codex | Publish PDC group discovery and state for configured consensus_groups even if no thermostat references them yet | e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.42 | codex | Add consensus_group dropdown listing existing groups in vTherm Add/Edit UI | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.43 | codex | Add hot/low valve outputs per thermostat with season-based logic (cool -> hot only, heat -> both), MQTT switches and real switch mapping fields | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.44 | codex | Add HOT/LOW consensus outputs for groups with MQTT switches and group UI fields for real switch mapping | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.45 | codex | Allow deleting consensus groups by clearing references from thermostats | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.46 | codex | Improve group delete check with normalized matching and list of referencing thermostats | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.48 | codex | Auto-save on group delete/clear to immediately remove MQTT discovery topics | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md
2026-03-25 | 2.6.49 | codex | Replace HOT/LOW group split with HEAT/COOL per-thermostat group mapping; remove hot/low topics and valve extras | e_therm_plus_ks/app/debug_server.py, e_therm_plus_ks/app/main.py, e_therm_plus_ks/config.yaml, e_therm_plus_ks/README_ADDON.md





