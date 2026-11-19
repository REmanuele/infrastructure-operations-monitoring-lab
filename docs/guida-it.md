# Guida rapida (italiano)

Questo primo prototipo serve a capire se il Raspberry e i suoi servizi locali rispondono correttamente. Inizia **senza modificare i servizi già attivi**.

## Prima prova, di sola lettura

Nella cartella del progetto sul Raspberry:

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py --config config.example.json --no-store
```

Il secondo comando controlla `ssh.service`, la porta locale 22, disco e memoria. Non scrive cronologia, non apre porte in ascolto, non cambia firewall o servizi. Se uno di questi controlli non è disponibile, il programma segnala `CRITICAL` o `UNKNOWN` anziché fingere che sia tutto a posto.

## Storico e incidenti

Per salvare lo storico localmente (non su GitHub):

```bash
python3 monitor.py --config config.example.json
```

La directory `runtime/` è esclusa da Git. Dopo due controlli consecutivi falliti viene registrato un incidente `OPEN`; quando il controllo torna positivo viene registrato `RECOVERED`.

## Aggiungere servizi del Raspberry

Copia `config.example.json` in `config.local.json` e configura controlli di tipo `systemd` con i nomi delle unità realmente presenti, e di tipo `tcp` per le porte `127.0.0.1` che vuoi verificare. Non inviare né pubblicare il file locale. Per la prima prova **non** installare timer, dashboard, agenti o container.

Il monitoraggio è incompleto finché non sono verificati i risultati sul Raspberry. Un servizio `active` o una porta TCP aperta non dimostrano la salute dell'applicazione; serviranno in seguito endpoint applicativi dedicati.

## HTTP locale (disponibile dalla v0.2)

Puoi aggiungere un endpoint HTTP **solo se conosci un percorso GET sicuro** dell'applicazione:

```json
{"id": "web-health", "type": "http", "host": "127.0.0.1", "port": 8080, "path": "/health", "expected_statuses": [200]}
```

Non sostituire automaticamente `8080` con porte reali: prima verifica che l'endpoint sia appropriato. Il tool non segue reindirizzamenti, non trasmette password e non legge il contenuto della risposta. Un codice HTTP 200 non dimostra che tutte le funzioni dell'applicazione siano sane. Anche un GET può produrre log lato server. Il pacchetto non installa o attiva alcun timer automaticamente.

## Nuovo in v0.3: storico con conservazione limitata

Ogni scansione salvata produce una riga nel file del giorno UTC, ad esempio `runtime/history-2026-11-19.jsonl`. Gli eventi di incidente finiscono in `runtime/incidents-2026-11-19.jsonl`. I file giornalieri più vecchi di 30 giorni vengono eliminati automaticamente durante le scansioni con salvataggio; puoi modificare la durata con `"retention_days": 30` nel file JSON (da 1 a 365). I vecchi `history.jsonl` e `incidents.jsonl` della v0.2 **non** vengono cancellati automaticamente.

Per consultare l'ultima scansione salvata e gli incidenti ancora aperti senza effettuare nuovi controlli:

```bash
python3 monitor.py --config config.local.json --summary
```

Il monitor di per sé non invia notifiche; per l'esecuzione periodica puoi usare gli esempi in `systemd/`, adattandoli al tuo ambiente.
