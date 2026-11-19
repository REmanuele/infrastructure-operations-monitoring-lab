# Version 0.3 — daily retention and saved status

Changes from v0.2:

- Each stored scan now appends to `runtime/history-YYYY-MM-DD.jsonl` for the UTC date. Incident events use `incidents-YYYY-MM-DD.jsonl`.
- `retention_days` (default 30) automatically prunes only expired daily log files; legacy v0.2 `history.jsonl` / `incidents.jsonl` are untouched.
- `--summary` prints last saved scan, counts by status, and open incidents **without running a scan or writing files**. Optional `--json`.
- Includes example systemd service and timer templates, with placeholders, not auto-installed.
- No network telemetry, no changes to monitored services.

## Upgrade caution

A live systemd timer should be paused during code replacement and restarted after tests. Preserve `config.local.json` and `runtime/` — neither is in this distribution. This release deliberately does not move/delete old runtime logs. The first v0.3 stored scan creates a dated history file.

## Checks

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py --config config.local.json --summary
python3 monitor.py --config config.local.json --no-store
```
