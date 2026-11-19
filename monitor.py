#!/usr/bin/env python3
"""Read-only Linux host and local service monitor (Python 3.10+, stdlib only)."""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import datetime as dt
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any, Iterator


CHECK_TYPES = {"systemd", "tcp", "http", "disk", "memory"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9_.@:-]+$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
HTTP_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~/-]*$")
DAILY_FILE_PATTERN = re.compile(r"^(history|incidents)-(\d{4}-\d{2}-\d{2})\.jsonl$")


class ConfigurationError(ValueError):
    pass


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{label} must be a number")
    number = float(value)
    if not 0 <= number <= 100:
        raise ConfigurationError(f"{label} must be between 0 and 100")
    return number


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigurationError("Configuration must be a JSON object")
    if not isinstance(config.get("host_label"), str) or not config["host_label"].strip():
        raise ConfigurationError("host_label must be a nonempty string")
    threshold = config.get("failure_threshold", 2)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 20:
        raise ConfigurationError("failure_threshold must be an integer between 1 and 20")
    retention = config.get("retention_days", 30)
    if isinstance(retention, bool) or not isinstance(retention, int) or not 1 <= retention <= 365:
        raise ConfigurationError("retention_days must be an integer between 1 and 365")
    timeout = config.get("timeout_seconds", 2)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 15:
        raise ConfigurationError("timeout_seconds must be between 0.1 and 15")
    state_dir = config.get("state_dir", "runtime")
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ConfigurationError("state_dir must be a nonempty string")
    checks = config.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ConfigurationError("checks must contain at least one check")
    if len(checks) > 100:
        raise ConfigurationError("checks may contain at most 100 items")
    ids = set()
    for i, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ConfigurationError(f"checks[{i}] must be an object")
        cid = check.get("id")
        if not isinstance(cid, str) or not ID_PATTERN.fullmatch(cid):
            raise ConfigurationError(f"checks[{i}].id is invalid")
        if cid in ids:
            raise ConfigurationError(f"Duplicate check id: {cid}")
        ids.add(cid)
        kind = check.get("type")
        if kind not in CHECK_TYPES:
            raise ConfigurationError(f"Invalid check type for {cid}: {kind}")
        if kind == "systemd":
            svc = check.get("service")
            if not isinstance(svc, str) or not SERVICE_PATTERN.fullmatch(svc) or svc.startswith("-"):
                raise ConfigurationError(f"Invalid systemd service name for {cid}")
        if kind in {"tcp", "http"}:
            if check.get("host") not in LOCAL_HOSTS:
                raise ConfigurationError(f"{kind.upper()} check {cid} only accepts loopback hosts")
            port = check.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ConfigurationError(f"{kind.upper()} check {cid} needs a valid port")
        if kind == "http":
            path = check.get("path", "/")
            if not isinstance(path, str) or len(path) > 128 or not HTTP_PATH_PATTERN.fullmatch(path) or path.startswith("//"):
                raise ConfigurationError(f"HTTP check {cid} needs a safe absolute path (no query string)")
            statuses = check.get("expected_statuses", [200])
            if (not isinstance(statuses, list) or not 1 <= len(statuses) <= 12
                or any(isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599 for value in statuses)
                or len(set(statuses)) != len(statuses)):
                raise ConfigurationError(f"HTTP check {cid} needs distinct expected_statuses between 100 and 599")
        if kind == "disk":
            if not isinstance(check.get("path"), str) or not check["path"].startswith("/"):
                raise ConfigurationError(f"Disk check {cid} needs an absolute path")
        if kind in {"memory", "disk"}:
            warning = _number(check.get("warning_percent"), f"{cid}.warning_percent")
            critical = _number(check.get("critical_percent"), f"{cid}.critical_percent")
            if not 0 <= warning < critical <= 100:
                raise ConfigurationError(f"{cid}: require warning_percent < critical_percent")
    return config


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot load configuration: {exc}") from exc
    validate_config(config)
    config["state_dir"] = str((path.resolve().parent / config.get("state_dir", "runtime")).resolve())
    return config


def _result(check: dict[str, Any], status: str, detail: str, metric: float | None = None) -> dict[str, Any]:
    result = {"id": check["id"], "type": check["type"], "status": status, "detail": detail}
    if metric is not None:
        result["percent"] = round(metric, 1)
    return result


def _usage_result(check: dict[str, Any], percent: float, noun: str) -> dict[str, Any]:
    if percent >= check["critical_percent"]:
        status = "CRITICAL"
    elif percent >= check["warning_percent"]:
        status = "WARNING"
    else:
        status = "OK"
    return _result(check, status, f"{noun}: {percent:.1f}% used", percent)


def _memory_percent() -> float:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, raw = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0])
    total, available = values["MemTotal"], values["MemAvailable"]
    if total <= 0 or available < 0 or available > total:
        raise ValueError("Invalid memory data")
    return 100 * (total - available) / total


def run_check(check: dict[str, Any], timeout: float) -> dict[str, Any]:
    kind = check["type"]
    if kind == "systemd":
        try:
            process = subprocess.run(
                ["systemctl", "is-active", "--", check["service"]],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _result(check, "UNKNOWN", f"Cannot query systemd ({type(exc).__name__})")
        combined_output = ((process.stdout or "") + " " + (process.stderr or "")).lower()
        if process.returncode != 0 and (
            "failed to connect to bus" in combined_output
            or "system has not been booted with systemd" in combined_output
        ):
            return _result(check, "UNKNOWN", "systemd manager unavailable")
        active = process.returncode == 0 and process.stdout.strip() == "active"
        return _result(check, "OK" if active else "CRITICAL", "service active" if active else "service not active")
    if kind == "tcp":
        try:
            with socket.create_connection((check["host"], check["port"]), timeout=timeout):
                pass
        except OSError:
            return _result(check, "CRITICAL", f"local TCP port {check['port']} not reachable")
        return _result(check, "OK", f"local TCP port {check['port']} reachable")
    if kind == "http":
        connection = http.client.HTTPConnection(check["host"], check["port"], timeout=timeout)
        try:
            # Never follow redirects, never send credentials, and never read response bodies.
            connection.request("GET", check.get("path", "/"), headers={"User-Agent": "local-operations-monitor/0.3", "Connection": "close"})
            response = connection.getresponse()
            status_code = response.status
        except (OSError, http.client.HTTPException) as exc:
            return _result(check, "CRITICAL", f"local HTTP request failed ({type(exc).__name__})")
        finally:
            connection.close()
        expected = check.get("expected_statuses", [200])
        status = "OK" if status_code in expected else "CRITICAL"
        return _result(check, status, f"local HTTP returned {status_code} (expected {', '.join(map(str, expected))})")
    if kind == "disk":
        try:
            usage = shutil.disk_usage(check["path"])
            if usage.total <= 0:
                raise ValueError("Invalid disk size")
            return _usage_result(check, 100 * usage.used / usage.total, "disk")
        except (OSError, ValueError) as exc:
            return _result(check, "UNKNOWN", f"Cannot read disk usage ({type(exc).__name__})")
    if kind == "memory":
        try:
            return _usage_result(check, _memory_percent(), "memory")
        except (OSError, KeyError, IndexError, ValueError) as exc:
            return _result(check, "UNKNOWN", f"Cannot read memory usage ({type(exc).__name__})")
    raise ValueError(f"Unknown check type: {kind}")


def evaluate(results: list[dict[str, Any]], previous: dict[str, Any], threshold: int, timestamp: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    current = {}
    events = []
    enriched = []
    for result in results:
        old = previous.get(result["id"], {})
        failures = max(0, int(old.get("consecutive_failures", 0)))
        was_open = old.get("incident_open") is True
        if result["status"] == "OK":
            failures = 0
            is_open = False
            incident_state = "RECOVERED" if was_open else "NONE"
            if was_open:
                events.append({"at": timestamp, "id": result["id"], "event": "RECOVERED"})
        else:
            failures += 1
            is_open = was_open or failures >= threshold
            incident_state = "OPEN" if is_open else "PENDING"
            if is_open and not was_open:
                events.append({"at": timestamp, "id": result["id"], "event": "OPEN", "status": result["status"]})
        current[result["id"]] = {"consecutive_failures": failures, "incident_open": is_open}
        enriched.append({**result, "consecutive_failures": failures, "incident_state": incident_state})
    return enriched, current, events


@contextlib.contextmanager
def lock_directory(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".monitor.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Cannot read monitoring state: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError("Monitoring state must be a JSON object")
    return state


def write_state(path: Path, state: dict[str, Any]) -> None:
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".state-", delete=False, encoding="utf-8") as file:
            temp_name = file.name
            json.dump(state, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(data, sort_keys=True) + "\n")


def daily_log_path(directory: Path, kind: str, day: dt.date) -> Path:
    """Split append-only log streams by UTC calendar day."""
    if kind not in {"history", "incidents"}:
        raise ValueError("Unknown log stream")
    return directory / f"{kind}-{day.isoformat()}.jsonl"


def prune_daily_logs(directory: Path, today: dt.date, retention_days: int) -> list[str]:
    """Only delete our own expired YYYY-MM-DD daily logs; leave legacy files untouched."""
    cutoff = today - dt.timedelta(days=retention_days - 1)
    removed = []
    for file in directory.iterdir():
        match = DAILY_FILE_PATTERN.fullmatch(file.name)
        if match is None or not file.is_file() or file.is_symlink():
            continue
        try:
            date = dt.date.fromisoformat(match.group(2))
        except ValueError:
            continue
        if date < cutoff:
            file.unlink()
            removed.append(file.name)
    return removed


def saved_summary(directory: Path) -> dict[str, Any] | None:
    """Read the latest saved scan without performing checks or writing files."""
    history_files = list(directory.glob("history-????-??-??.jsonl"))
    legacy = directory / "history.jsonl"
    if legacy.is_file():
        history_files.append(legacy)
    history_files = [p for p in history_files if p.is_file() and not p.is_symlink()]
    history_files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    report = None
    for path in history_files:
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if line.strip():
                report = json.loads(line)
                break
        if report is not None:
            break
    if report is None:
        return None
    if not isinstance(report, dict) or not isinstance(report.get("checks"), list):
        raise ValueError("Last monitoring report has invalid structure")
    state = read_state(directory / "state.json")
    counts = Counter(check["status"] for check in report["checks"])
    opened = sorted(cid for cid, data in state.items() if isinstance(data, dict) and data.get("incident_open") is True)
    return {
        "at": report["at"],
        "host_label": report["host_label"],
        "checks": len(report["checks"]),
        "status_counts": dict(counts),
        "open_incidents": opened,
    }


def run_monitor(config: dict[str, Any], *, store: bool = True) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    timestamp = now.isoformat(timespec="seconds")
    state_dir = Path(config["state_dir"])
    def execute(previous: dict[str, Any]) -> dict[str, Any]:
        results = [run_check(c, float(config.get("timeout_seconds", 2))) for c in config["checks"]]
        checked, current, events = evaluate(results, previous, config.get("failure_threshold", 2), timestamp)
        report = {"at": timestamp, "host_label": config["host_label"], "checks": checked, "events": events}
        if store:
            write_state(state_dir / "state.json", current)
            append_jsonl(daily_log_path(state_dir, "history", now.date()), report)
            for event in events:
                append_jsonl(daily_log_path(state_dir, "incidents", now.date()), event)
            prune_daily_logs(state_dir, now.date(), config.get("retention_days", 30))
        return report
    if store:
        with lock_directory(state_dir):
            return execute(read_state(state_dir / "state.json"))
    return execute({})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Linux and local service monitoring")
    parser.add_argument("--config", default="config.example.json", help="Path to JSON configuration")
    parser.add_argument("--json", action="store_true", help="Output full report as JSON")
    parser.add_argument("--no-store", action="store_true", help="Do not write history or incident state")
    parser.add_argument("--strict", action="store_true", help="Nonzero exit code when checks are degraded")
    parser.add_argument("--summary", action="store_true", help="Display last stored scan and open incidents without running checks")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(Path(args.config))
        if args.summary:
            summary = saved_summary(Path(config["state_dir"])) if Path(config["state_dir"]).exists() else None
            if summary is None:
                print("No stored monitoring scans yet.")
                return 0
            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                counts = ", ".join(f"{key}={value}" for key, value in sorted(summary["status_counts"].items()))
                print(f"Last scan: {summary['at']} | host={summary['host_label']}")
                print(f"Checks: {summary['checks']} | {counts}")
                print(f"Open incidents: {len(summary['open_incidents'])}")
                for check_id in summary["open_incidents"]:
                    print(f"  - {check_id}")
            return 0
        report = run_monitor(config, store=not args.no_store)
    except (ConfigurationError, OSError, ValueError) as exc:
        print(f"MONITOR ERROR: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Host: {report['host_label']} | {report['at']}")
        for check in report["checks"]:
            print(f"[{check['status']:<8}] {check['id']}: {check['detail']} | incident={check['incident_state']}")
        for event in report["events"]:
            print(f"EVENT: {event['id']} {event['event']}")
    if args.strict:
        statuses = {c["status"] for c in report["checks"]}
        if statuses & {"CRITICAL", "UNKNOWN"}:
            return 2
        if "WARNING" in statuses:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
