# e-Therm Plus KS - README (addon-specific)

File alternativo al `README.md` esistente: contenuto minimale e istruzioni operative.

Scopo
- Tracciare velocemente lo scopo dell'addon e le regole di versioning.

Versione corrente
- `config.yaml` -> `version`: `2.6.200`

Ultima modifica
- Dettaglio termostato: posizione iniziale del pomello setpoint renderizzata lato server per evitare il salto all'apertura.
- MQTT: ignorati in silenzio i comandi retained vuoti sui topic `/set`.

Regole
- Aggiornare `config.yaml` -> `version` ad ogni modifica rilevante.
- Aggiungere una voce in `worklog.md` con data ISO, versione, autore, breve descrizione e file modificati.

Vedi `worklog.md` per il passaggio di consegne e il resoconto completo.
