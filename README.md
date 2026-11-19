# Infrastructure Operations & Monitoring Lab

A read-only, **working prototype** for monitoring a Linux host and local services. Built for a Raspberry Pi 4 running Debian Linux, but usable on other Linux machines with systemd. Uses only Python's standard library (Python 3.10+).

## Problem

A machine can be up while one of its applications is down. Checking `systemctl` manually or noticing a stopped TCP listener too late makes troubleshooting reactive. This tool takes a consistent snapshot of local service state and basic resource pressure, and records incident transitions across runs.

## What works in v0.3

- Query the real state of selected systemd services (read-only).
- Check whether a **local TCP listener** accepts connections; a TCP connection does **not** prove application-level health.
- Probe **local HTTP endpoints** with GET and compare response status against explicitly configured expected codes; never follow redirects.
- Evaluate `/` disk usage and memory pressure against configured warning/critical levels.
- Persist JSONL history and raise a single `OPEN` incident event after N consecutive failed checks; record `RECOVERED` when a check becomes healthy.
- Accept a local JSON configuration and print concise text or JSON output.
- Unit and integration tests without third-party packages.

**Not bundled or enabled by default:** systemd scheduling (see examples), external notifications, metrics dashboards, database-consistent backups. This repository is a lab implementation, not a claim of production readiness.

## Quick start (Linux)

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py --config config.example.json
python3 monitor.py --config config.example.json --json
```

Example configuration checks `ssh.service`, TCP `127.0.0.1:22`, local root filesystem and memory. If SSH is disabled, change the service and port in your **local** copy of the config. Running the monitor does **not** restart services or open firewall ports. It connects only to the machine's loopback interface.

To customize checks, create a separate, **untracked** configuration:

```bash
cp config.example.json config.local.json
# Edit config.local.json locally, then:
python3 monitor.py --config config.local.json
```

> Do not commit `config.local.json`, `runtime/`, real service names, private IPs, report output, tokens or production configurations. `.gitignore` excludes the local files.

Example output (illustrative, not a real host result):

```text
Host: linux-lab | 2026-11-19T12:00:00+00:00
[OK      ] ssh-service: service active | incident=NONE
[OK      ] ssh-local-port: local TCP port 22 reachable | incident=NONE
[OK      ] root-disk: disk: 19.5% used | incident=NONE
[OK      ] memory: memory: 25.7% used | incident=NONE
```

## HTTP check (v0.2)

The HTTP check is deliberately **loopback only** and uses plain HTTP. The tool does not follow redirects, send credentials or inspect response bodies. It considers `200` healthy by default. For an application with an authenticated homepage redirect, explicitly list the allowed status codes; a redirect merely confirms that the HTTP server responded, not that the app is healthy.

Example public configuration (for testing only; no application needs to be running at this port):

```json
{"id": "web-health", "type": "http", "host": "127.0.0.1", "port": 8080, "path": "/health", "expected_statuses": [200]}
```

Add HTTP checks in your untracked `config.local.json` only **after** confirming which GET endpoints are safe to request. Do not add secret tokens or query parameters: paths reject query strings. A GET request can still generate application access logs. The HTTP check reports HTTP response status, not database readiness or business functionality.

## Runtime files

`runtime/state.json` tracks consecutive failures. Logs are segmented by **UTC day** into `runtime/history-YYYY-MM-DD.jsonl` (one complete scan per line) and `runtime/incidents-YYYY-MM-DD.jsonl` (OPEN/RECOVERED events). On a stored scan, daily log files outside `retention_days` are removed (default: 30 days, configurable from 1 to 365). Only exactly matching dated log files are pruned; `state.json` and other files are left untouched. Previous v0.2 files `runtime/history.jsonl` and `runtime/incidents.jsonl` are **preserved but no longer appended or automatically pruned**. Review them before choosing whether to archive or delete them yourself.

See the most recent saved scan and currently open incidents **without executing checks or writing to disk**:

```bash
python3 monitor.py --config config.local.json --summary
python3 monitor.py --config config.local.json --summary --json
```

Runtime files may contain private service names and host information. No telemetry is sent to a cloud service. Do not publish runtime data.

## Exit codes

By default exit `0` means the monitoring **command** executed, even if checks are degraded. Use `--strict` for exit `1` (warning), `2` (critical/unknown), or `3` (configuration/state error). `--no-store` performs a one-shot run without writing state/history; incident streaks reset with each such run.

## Safety and limitations

- Read-only with respect to services and OS configuration. Local runtime files are written only when storage is enabled.
- A missing `systemctl` or unreadable metric is `UNKNOWN`, not falsely `OK`.
- TCP and HTTP checks are deliberately restricted to `127.0.0.1`, `localhost`, or `::1`; this is **not** a network scanner.
- An incident is only generated after the configured failure threshold. Run at a regular interval for this mechanism to be meaningful; scheduling examples are in `systemd/`, but enabling them requires an explicit, local administrator action.
- An HTTP status check is a limited application-layer probe, not a substitute for end-to-end functional checks, backups, restore drills or production monitoring.
- The monitor can generate runtime data containing service names; keep these files private. The default 30-day log retention deliberately deletes old daily logs, but never removes the state file or v0.2 legacy logs.

See [operations guide](docs/operations.md), [privacy notes](docs/security.md), and the [Italian quick start](docs/guida-it.md).

## Automatic runs (optional)

`systemd/` contains **templates** for a oneshot service and five-minute timer. They intentionally use placeholders for the Linux username and installation location; do not install unchanged. Test manual operation and backup plans before enabling on a host. The monitor writes local files only, and does not send notifications. The runtime state is deliberately not included in this repository.
