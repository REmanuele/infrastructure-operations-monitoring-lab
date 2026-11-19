# Operations and verification

## Local verification without interrupting anything

1. Run `python3 -m unittest discover -s tests -v`.
2. Run `python3 monitor.py --config config.example.json --no-store` to inspect SSH, local TCP, disk and memory without writing monitoring data.
3. Run the command without `--no-store` twice, then inspect `runtime/history-YYYY-MM-DD.jsonl` (UTC day). The script creates these files; it does not modify existing services.
4. To monitor additional services, copy `config.example.json` to `config.local.json` and add systemd checks for your own unit names (obtained separately with `systemctl list-units --type=service`). Never publish that file.

## Non-disruptive failure and recovery test

Do **not** stop a real service to exercise alerts. Instead, use a temporary local config with a TCP check on an unused loopback port and `failure_threshold: 2`. Run the monitor twice: the first run reports `PENDING`, the second `OPEN`. Then point **that same check ID** at a locally listening port and run again: it will report `RECOVERED`. This tests the state machine without touching services. Only do this in a private config file.

Alternatively, run unit tests, which mock all system interactions and verify both transitions.

## Interpreting checks

- `OK`: the observed check passed at sampling time.
- `WARNING`: resource usage meets the configured warning threshold.
- `CRITICAL`: a service/port failed or a resource meets the critical threshold.
- `UNKNOWN`: insufficient information to evaluate a check.
- `PENDING`: problem observed, but not yet repeated enough to open an incident.
- `OPEN`: an incident is already open or just opened.
- `RECOVERED`: a previously open incident became healthy.

A TCP listener can accept connections even when its application is not healthy; v0.2 supports HTTP status checks on known safe local GET endpoints; these are not end-to-end health checks.

## Roadmap, conditional on verification

1. Verify behavior on a real Raspberry Pi in manual, read-only runs.
2. Add dedicated HTTP `/health` checks for applications that expose them (implemented as optional local check in v0.2).
3. Only after backup and owner approval, consider a systemd timer and alert delivery.
4. Add monitoring dashboards and incident documentation from synthetic or sanitized data.

## Retention and current status (v0.3)

Daily logs rotate by UTC date. `retention_days` (default 30) controls removal of older **dated** JSONL history and incident files on each stored scan. Never automatically delete a legacy v0.2 `history.jsonl`/`incidents.jsonl`; they remain in place and should be reviewed manually after a successful upgrade. `state.json` is retained indefinitely as the incident state machine input.

Inspect the most recent saved snapshot without launching a scan:

```bash
python3 monitor.py --config config.local.json --summary
```

This does not replace external alerting or off-device backups. A systemd oneshot service should exit 0 for successful execution even when individual checks report CRITICAL; use `--strict` only when nonzero exit codes are intentionally handled.
