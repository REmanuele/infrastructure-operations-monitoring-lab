# v0.2 — Local HTTP checks

## What changed
- Added opt-in `http` check using Python stdlib `http.client`.
- Only loopback hosts are accepted; no redirects, credentials or response-body reading.
- Status-code allowlist (`expected_statuses`) defaults to `[200]`.
- Added validation and real local HTTP integration tests; no external HTTP requests.

## Upgrade from v0.1
Extract the ZIP into the home directory as in the original instructions. The ZIP has a top-level `infrastructure-operations-monitoring-lab/` folder. It intentionally does **not** contain `config.local.json` or `runtime/` and will not overwrite those files.

```sh
cd ~/infrastructure-operations-monitoring-lab
python3 -m unittest discover -s tests -v
python3 monitor.py --config config.local.json --no-store
```

No services are started, stopped or modified by the upgrade. Scheduling and privileged changes are not enabled.

## Future checks
- Confirm a safe GET endpoint for each application; not all apps expose `/health`.
- Add HTTP checks in the untracked local config after consent and manual endpoint validation.
- After backup strategy is completed, plan opt-in periodic execution and retention of history.
