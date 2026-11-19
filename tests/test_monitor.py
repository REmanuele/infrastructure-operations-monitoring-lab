import contextlib
import io
import json
import http.server
import threading
import socket
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import monitor


SAMPLE = {
    "host_label": "test-node",
    "state_dir": "runtime",
    "failure_threshold": 2,
    "timeout_seconds": 1,
    "checks": [
        {"id": "ssh", "type": "systemd", "service": "ssh.service"},
        {"id": "tcp", "type": "tcp", "host": "127.0.0.1", "port": 22},
        {"id": "disk", "type": "disk", "path": "/", "warning_percent": 80, "critical_percent": 90},
        {"id": "mem", "type": "memory", "warning_percent": 80, "critical_percent": 90},
    ],
}


class ConfigurationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(monitor.validate_config(SAMPLE)["host_label"], "test-node")

    def test_duplicate_id(self):
        copy = json.loads(json.dumps(SAMPLE))
        copy["checks"].append(copy["checks"][0])
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(copy)

    def test_reject_external_tcp(self):
        copy = json.loads(json.dumps(SAMPLE))
        copy["checks"][1]["host"] = "8.8.8.8"
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(copy)

    def test_invalid_port(self):
        copy = json.loads(json.dumps(SAMPLE))
        copy["checks"][1]["port"] = 0
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(copy)

    def test_invalid_thresholds(self):
        copy = json.loads(json.dumps(SAMPLE))
        copy["checks"][2]["warning_percent"] = 99
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(copy)

    def test_invalid_service_name(self):
        copy = json.loads(json.dumps(SAMPLE))
        copy["checks"][0]["service"] = "--no-pager"
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(copy)

    def test_empty_checks(self):
        copy = json.loads(json.dumps(SAMPLE))
        copy["checks"] = []
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(copy)

    def test_config_relative_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(SAMPLE), encoding="utf-8")
            result = monitor.load_config(path)
            self.assertEqual(Path(result["state_dir"]), Path(temp) / "runtime")


class CheckTests(unittest.TestCase):
    @patch("monitor.subprocess.run")
    def test_service_active(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="active\n", stderr="")
        result = monitor.run_check(SAMPLE["checks"][0], 1)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(run.call_args.args[0], ["systemctl", "is-active", "--", "ssh.service"])

    @patch("monitor.subprocess.run")
    def test_service_inactive(self, run):
        run.return_value = subprocess.CompletedProcess([], 3, stdout="inactive\n", stderr="")
        self.assertEqual(monitor.run_check(SAMPLE["checks"][0], 1)["status"], "CRITICAL")

    @patch("monitor.subprocess.run", side_effect=FileNotFoundError)
    def test_service_unknown(self, run):
        self.assertEqual(monitor.run_check(SAMPLE["checks"][0], 1)["status"], "UNKNOWN")

    @patch("monitor.subprocess.run")
    def test_systemd_manager_not_available_is_unknown(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="Failed to connect to bus: Host is down")
        self.assertEqual(monitor.run_check(SAMPLE["checks"][0], 1)["status"], "UNKNOWN")

    @patch("monitor.socket.create_connection")
    def test_tcp_success(self, connect):
        self.assertEqual(monitor.run_check(SAMPLE["checks"][1], 1)["status"], "OK")
        connect.assert_called_once_with(("127.0.0.1", 22), timeout=1)

    @patch("monitor.socket.create_connection", side_effect=ConnectionRefusedError)
    def test_tcp_failure(self, connect):
        self.assertEqual(monitor.run_check(SAMPLE["checks"][1], 1)["status"], "CRITICAL")

    def test_real_loopback_listener_open_then_closed(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            check = {"id": "live-loopback", "type": "tcp", "host": "127.0.0.1", "port": port}
            self.assertEqual(monitor.run_check(check, 1)["status"], "OK")
        self.assertEqual(monitor.run_check(check, 1)["status"], "CRITICAL")

    @patch("monitor.shutil.disk_usage")
    def test_disk_warning(self, usage):
        usage.return_value = shutil_usage(100, 85, 15)
        result = monitor.run_check(SAMPLE["checks"][2], 1)
        self.assertEqual((result["status"], result["percent"]), ("WARNING", 85))

    @patch("monitor._memory_percent", return_value=95)
    def test_memory_critical(self, metric):
        self.assertEqual(monitor.run_check(SAMPLE["checks"][3], 1)["status"], "CRITICAL")

    @patch("monitor._memory_percent", side_effect=ValueError)
    def test_memory_unknown(self, metric):
        self.assertEqual(monitor.run_check(SAMPLE["checks"][3], 1)["status"], "UNKNOWN")


def shutil_usage(total, used, free):
    return type("Usage", (), {"total": total, "used": used, "free": free})()


class IncidentTests(unittest.TestCase):
    def check(self, status):
        return [{"id": "example", "type": "tcp", "status": status, "detail": "example"}]

    def test_pending_open_no_duplicate_and_recovery(self):
        result, state, events = monitor.evaluate(self.check("CRITICAL"), {}, 2, "t1")
        self.assertEqual(result[0]["incident_state"], "PENDING")
        self.assertFalse(events)
        result, state, events = monitor.evaluate(self.check("CRITICAL"), state, 2, "t2")
        self.assertEqual([e["event"] for e in events], ["OPEN"])
        result, state, events = monitor.evaluate(self.check("CRITICAL"), state, 2, "t3")
        self.assertEqual(result[0]["incident_state"], "OPEN")
        self.assertEqual(events, [])
        result, state, events = monitor.evaluate(self.check("OK"), state, 2, "t4")
        self.assertEqual(result[0]["incident_state"], "RECOVERED")
        self.assertEqual([e["event"] for e in events], ["RECOVERED"])
        result, state, events = monitor.evaluate(self.check("OK"), state, 2, "t5")
        self.assertEqual(events, [])

    def test_warning_counts(self):
        result, state, _ = monitor.evaluate(self.check("WARNING"), {}, 2, "t1")
        result, _, events = monitor.evaluate(self.check("WARNING"), state, 2, "t2")
        self.assertEqual(events[0]["status"], "WARNING")

    def test_unknown_counts_as_problem(self):
        result, state, _ = monitor.evaluate(self.check("UNKNOWN"), {}, 2, "t1")
        self.assertEqual(state["example"]["consecutive_failures"], 1)


class PersistenceTests(unittest.TestCase):
    def test_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            monitor.write_state(path, {"a": {"consecutive_failures": 2}})
            self.assertEqual(monitor.read_state(path)["a"]["consecutive_failures"], 2)

    def test_two_runs_persist_incident(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {**SAMPLE, "state_dir": temp, "checks": [SAMPLE["checks"][1]]}
            with patch("monitor.run_check", return_value={"id": "tcp", "type": "tcp", "status": "CRITICAL", "detail": "closed"}):
                first = monitor.run_monitor(config)
                second = monitor.run_monitor(config)
            self.assertEqual(first["events"], [])
            self.assertEqual(second["events"][0]["event"], "OPEN")
            day = monitor.dt.datetime.now(monitor.dt.timezone.utc).date()
            self.assertEqual(len(monitor.daily_log_path(Path(temp), "history", day).read_text().splitlines()), 2)
            self.assertEqual(len(monitor.daily_log_path(Path(temp), "incidents", day).read_text().splitlines()), 1)

    def test_no_store_has_no_files(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "runtime"
            config = {**SAMPLE, "state_dir": str(target), "checks": [SAMPLE["checks"][1]]}
            with patch("monitor.run_check", return_value={"id": "tcp", "type": "tcp", "status": "OK", "detail": "open"}):
                monitor.run_monitor(config, store=False)
            self.assertFalse(target.exists())

    def test_cli_json_no_store(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {**SAMPLE, "checks": [{"id": "memory", "type": "memory", "warning_percent": 80, "critical_percent": 90}]}
            path = Path(temp) / "sample.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("monitor._memory_percent", return_value=12):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = monitor.main(["--config", str(path), "--no-store", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["checks"][0]["status"], "OK")

    def test_cli_strict_critical(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {**SAMPLE, "checks": [{"id": "memory", "type": "memory", "warning_percent": 80, "critical_percent": 90}]}
            path = Path(temp) / "sample.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("monitor._memory_percent", return_value=95):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = monitor.main(["--config", str(path), "--no-store", "--strict"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()


class HTTPConfigurationTests(unittest.TestCase):
    def example(self):
        value = json.loads(json.dumps(SAMPLE))
        value["checks"] = [{"id": "app-http", "type": "http", "host": "127.0.0.1", "port": 8080, "path": "/health", "expected_statuses": [200, 204]}]
        return value

    def test_valid(self):
        self.assertEqual(monitor.validate_config(self.example())["checks"][0]["type"], "http")

    def test_default_path_and_status(self):
        value = self.example()
        del value["checks"][0]["path"]
        del value["checks"][0]["expected_statuses"]
        monitor.validate_config(value)

    def test_reject_external_http(self):
        value = self.example()
        value["checks"][0]["host"] = "example.com"
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(value)

    def test_reject_invalid_port(self):
        value = self.example()
        value["checks"][0]["port"] = 65536
        with self.assertRaises(monitor.ConfigurationError):
            monitor.validate_config(value)

    def test_reject_path_query_and_fragment(self):
        for path in ("http://example.com", "//example.com", "/health?token=secret", "/health#x", "/\r\nHeader:x", "/" + "a" * 129):
            value = self.example()
            value["checks"][0]["path"] = path
            with self.subTest(path=repr(path)), self.assertRaises(monitor.ConfigurationError):
                monitor.validate_config(value)

    def test_reject_invalid_statuses(self):
        for statuses in ([], [200, 200], [True], [99], [600], "200", [200, "404"]):
            value = self.example()
            value["checks"][0]["expected_statuses"] = statuses
            with self.subTest(statuses=statuses), self.assertRaises(monitor.ConfigurationError):
                monitor.validate_config(value)


class HTTPIntegrationTests(unittest.TestCase):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "https://example.invalid/should-not-fetch")
            else:
                self.send_response(503)
            self.end_headers()
        def log_message(self, fmt, *args):
            pass

    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def check(self, path, expected=None):
        check = {"id": "web", "type": "http", "host": "127.0.0.1", "port": self.port, "path": path}
        if expected is not None:
            check["expected_statuses"] = expected
        monitor.validate_config({"host_label": "t", "checks": [check]})
        return monitor.run_check(check, 1)

    def test_health_ok(self):
        result = self.check("/health")
        self.assertEqual(result["status"], "OK")
        self.assertIn("200", result["detail"])

    def test_service_unavailable_status(self):
        self.assertEqual(self.check("/not-found")["status"], "CRITICAL")

    def test_redirect_not_followed(self):
        self.assertEqual(self.check("/redirect")["status"], "CRITICAL")
        self.assertEqual(self.check("/redirect", [200, 302])["status"], "OK")

    def test_connection_refused(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        check = {"id": "web", "type": "http", "host": "127.0.0.1", "port": port, "path": "/"}
        self.assertEqual(monitor.run_check(check, 0.5)["status"], "CRITICAL")


class RetentionTests(unittest.TestCase):
    def test_retention_default_and_validation(self):
        monitor.validate_config(SAMPLE)
        for value in (0, 366, -1, 1.5, True, "30"):
            config = {**SAMPLE, "retention_days": value}
            with self.subTest(value=value), self.assertRaises(monitor.ConfigurationError):
                monitor.validate_config(config)
        monitor.validate_config({**SAMPLE, "retention_days": 30})

    def test_prunes_only_own_expired_dated_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for name in ("history-2026-10-20.jsonl", "history-2026-10-21.jsonl",
                         "incidents-2026-10-20.jsonl", "incidents-2026-10-21.jsonl",
                         "history.jsonl", "incidents.jsonl", "state.json",
                         "history-2026-99-99.jsonl", "README.txt"):
                (p/name).write_text("keep?", encoding="utf-8")
            removed = monitor.prune_daily_logs(p, monitor.dt.date(2026, 11, 19), 30)
            self.assertEqual(set(removed), {"history-2026-10-20.jsonl", "incidents-2026-10-20.jsonl"})
            for name in ("history-2026-10-21.jsonl", "incidents-2026-10-21.jsonl",
                         "history.jsonl", "incidents.jsonl", "state.json", "README.txt", "history-2026-99-99.jsonl"):
                self.assertTrue((p/name).exists(), name)

    def test_no_store_does_not_prune_old_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            old = p / "history-2020-01-01.jsonl"
            old.write_text("old\n", encoding="utf-8")
            conf = {**SAMPLE, "state_dir": tmp, "checks": [SAMPLE["checks"][1]]}
            with patch("monitor.run_check", return_value={"id": "tcp", "type": "tcp", "status": "OK", "detail": "fine"}):
                monitor.run_monitor(conf, store=False)
            self.assertEqual(old.read_text(), "old\n")

    def test_store_rotates_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            stale = p / "history-2020-01-01.jsonl"
            stale.write_text("old\n", encoding="utf-8")
            conf = {**SAMPLE, "state_dir": tmp, "checks": [SAMPLE["checks"][1]], "retention_days": 30}
            with patch("monitor.run_check", return_value={"id": "tcp", "type": "tcp", "status": "OK", "detail": "fine"}):
                monitor.run_monitor(conf)
            today = monitor.dt.datetime.now(monitor.dt.timezone.utc).date()
            self.assertTrue(monitor.daily_log_path(p, "history", today).exists())
            self.assertFalse(stale.exists())
            self.assertTrue((p/"state.json").exists())


class SummaryTests(unittest.TestCase):
    def test_summary_no_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            report = {"at": "2026-11-19T10:00:00+00:00", "host_label": "t", "checks": [
                {"id": "x", "status": "OK"}, {"id": "y", "status": "CRITICAL"}]}
            monitor.append_jsonl(p/"history-2026-11-19.jsonl", report)
            monitor.write_state(p/"state.json", {"y": {"incident_open": True, "consecutive_failures": 2}})
            before = sorted((x.name, x.stat().st_size) for x in p.iterdir())
            result = monitor.saved_summary(p)
            after = sorted((x.name, x.stat().st_size) for x in p.iterdir())
            self.assertEqual(before, after)
            self.assertEqual(result["open_incidents"], ["y"])
            self.assertEqual(result["status_counts"], {"OK": 1, "CRITICAL": 1})

    def test_legacy_file_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            monitor.append_jsonl(p/"history.jsonl", {"at": "2026-11-19T10:00:00+00:00", "host_label": "old", "checks": [{"id": "x", "status": "OK"}]})
            self.assertEqual(monitor.saved_summary(p)["host_label"], "old")
            self.assertEqual(monitor.saved_summary(p)["checks"], 1)

    def test_summary_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(monitor.saved_summary(Path(tmp)))

    def test_summary_cli_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {**SAMPLE, "state_dir": "runtime"}
            p = Path(tmp)/"config.json"
            p.write_text(json.dumps(config), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = monitor.main(["--config", str(p), "--summary"])
            self.assertEqual(rc, 0)
            self.assertIn("No stored monitoring scans yet", out.getvalue())
            self.assertFalse((Path(tmp)/"runtime").exists())
