#!/usr/bin/env python3
"""Platform-independent host watchdog tests; Docker and time are always fake."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("guardian_watchdog", ROOT / "deploy" / "guardian-watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now


class Crash(BaseException):
    pass


class FakeDocker:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.checks = []
        self.states = {"astrbot": True, "napcat": True, "other": True}
        self.failures = {}
        self.hook = None
        self.stop_delay = 0

    def action(self, operation, target):
        self.calls.append((operation, target))
        # A failed stop/restart can nevertheless have stopped its target.
        if operation in ("stop", "restart"):
            self.states[target] = False
        if self.hook:
            self.hook(operation, target)
        failures = self.failures.get((operation, target), 0)
        if failures:
            self.failures[(operation, target)] = failures - 1
            return False
        if operation in ("start", "restart"):
            self.states[target] = True
        if operation == "stop":
            self.clock.now += self.stop_delay
        return True

    def running(self, target):
        self.checks.append(target)
        return self.states.get(target, False)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="guardian-watchdog-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.clock = Clock()
        self.config = wd.Config({
            "GUARDIAN_COMPOSE_DIR": str(self.directory),
            "GUARDIAN_REQUEST_FILE": str(self.directory / "request.json"),
            "GUARDIAN_RESULT_FILE": str(self.directory / "result.json"),
            "GUARDIAN_LOG_FILE": str(self.directory / "watchdog.log"),
            "GUARDIAN_ALLOWED_TARGETS": "astrbot napcat other",
        })
        self.docker = FakeDocker(self.clock)
        self.watchdog = self.recreate()

    def recreate(self, persist=wd.atomic_json):
        return wd.Watchdog(self.config, self.docker, self.clock, persist)

    def request(self, nonce="n1", duration=1, targets=None, legacy=False, **changes):
        payload = {"nonce": nonce, "ts": self.clock(), "reason": "test"}
        if legacy:
            payload["target"] = "napcat"
        else:
            payload.update(version=2, operation="sleep", duration_min=duration,
                           targets=["astrbot", "napcat"] if targets is None else targets)
        payload.update(changes)
        wd.atomic_json(self.config.request, payload)
        return payload

    def result(self):
        return json.loads(self.config.result.read_text(encoding="utf-8"))

    def ledger(self):
        return json.loads(self.config.state.read_text(encoding="utf-8"))

    def test_legacy_restart_success_and_result_schema(self):
        self.request(legacy=True)
        result = self.watchdog.tick()
        self.assertEqual(self.docker.calls, [("restart", "napcat")])
        self.assertEqual(result["detail"], "restarted")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["ok"])
        self.assertEqual(result["target"], "napcat")
        self.assertEqual(result["targets"], ["napcat"])
        self.assertEqual(result["operation"], "restart")
        self.assertEqual(set(result), {"nonce", "ts", "ok", "status", "targets",
                                      "target", "detail", "wake_at", "operation"})
        self.assertIsNone(self.ledger()["active"])
        self.assertFalse(self.config.request.exists())
        self.assertFalse(self.config.processing.exists())

    def test_legacy_restart_failure_recovers_but_reports_failed(self):
        self.docker.failures[("restart", "napcat")] = 1
        self.request(legacy=True)
        result = self.watchdog.tick()
        self.assertEqual(self.docker.calls, [("restart", "napcat"), ("start", "napcat")])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertTrue(self.docker.states["napcat"])
        self.assertIsNone(self.ledger()["active"])

    def test_legacy_failed_recovery_retains_obligation(self):
        self.docker.failures[("restart", "napcat")] = 1
        self.docker.failures[("start", "napcat")] = 1
        self.request(legacy=True)
        self.assertEqual(self.watchdog.tick()["status"], "recovery_pending")
        self.assertIsNotNone(self.ledger()["active"])
        self.assertEqual(self.recreate().tick()["status"], "failed")
        self.assertIsNone(self.ledger()["active"])

    def test_zero_duration_restarts_whole_group(self):
        self.request(duration=0)
        result = self.watchdog.tick()
        self.assertEqual(self.docker.calls, [("restart", "astrbot"), ("restart", "napcat")])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["operation"], "sleep")
        self.assertEqual(result["detail"], "restarted")

    def test_group_sleep_deadline_is_persisted_before_each_stop(self):
        self.docker.stop_delay = 4
        observed = []

        def check(operation, target):
            if operation == "stop":
                active = self.ledger()["active"]
                self.assertIn(target, active["attempted"])
                self.assertEqual(active["phase"], "stopping")
                self.assertEqual(active["wake_at"], 10060)
                self.assertIn("n1", self.ledger()["seen"])
                observed.append(target)
        self.docker.hook = check
        self.request()
        result = self.watchdog.tick()
        self.assertEqual(observed, ["astrbot", "napcat"])
        self.assertEqual(result["status"], "sleeping")
        self.assertFalse(result["ok"])
        self.assertEqual(result["wake_at"], 10068)
        self.assertEqual(self.ledger()["active"]["phase"], "sleeping")

    def test_process_recreation_resumes_saved_deadline_and_wakes_on_time(self):
        self.request(duration=2)
        self.watchdog.tick()
        self.clock.now += 119
        self.assertEqual(self.recreate().tick()["status"], "sleeping")
        self.assertEqual(self.docker.calls, [("stop", "astrbot"), ("stop", "napcat")])
        self.clock.now += 1
        result = self.recreate().tick()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.calls[-2:], [("start", "astrbot"), ("start", "napcat")])
        self.assertIsNone(self.ledger()["active"])

    def test_overdue_job_wakes_immediately_without_sleep(self):
        self.request()
        self.watchdog.tick()
        self.clock.now += 1000
        with patch.object(wd.time, "sleep", side_effect=AssertionError("no blocking wait")):
            result = self.recreate().tick()
        self.assertEqual(result["status"], "completed")

    def test_partial_stop_recovers_failed_target_and_leaves_unattempted_untouched(self):
        self.docker.failures[("stop", "napcat")] = 1
        self.request(targets=["astrbot", "napcat", "other"])
        result = self.watchdog.tick()
        self.assertEqual(self.docker.calls, [("stop", "astrbot"), ("stop", "napcat"),
                                           ("start", "astrbot"), ("start", "napcat")])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertTrue(all(self.docker.states.values()))
        self.assertIsNone(self.ledger()["active"])

    def test_first_stop_failure_still_starts_failed_target(self):
        self.docker.failures[("stop", "astrbot")] = 1
        self.request()
        self.assertEqual(self.watchdog.tick()["status"], "failed")
        self.assertEqual(self.docker.calls, [("stop", "astrbot"), ("start", "astrbot")])

    def test_failed_start_retries_later_without_restarting_successful_target(self):
        self.request()
        self.watchdog.tick()
        self.clock.now += 60
        self.docker.failures[("start", "napcat")] = 1
        result = self.recreate().tick()
        self.assertEqual(result["status"], "recovery_pending")
        self.assertFalse(result["ok"])
        self.assertEqual(self.ledger()["active"]["started"], ["astrbot"])
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls.count(("start", "astrbot")), 1)
        self.assertEqual(self.docker.calls.count(("start", "napcat")), 2)

    def test_start_success_without_running_verification_is_not_completion(self):
        self.request()
        self.watchdog.tick()
        self.clock.now += 60
        running = self.docker.running
        self.docker.running = lambda target: target != "napcat" and running(target)
        self.assertEqual(self.recreate().tick()["status"], "recovery_pending")
        self.assertEqual(self.ledger()["active"]["started"], ["astrbot"])
        self.docker.running = running
        self.assertEqual(self.recreate().tick()["status"], "completed")

    def test_previously_started_target_is_reverified_before_completion(self):
        self.request()
        self.watchdog.tick()
        self.clock.now += 60
        self.docker.failures[("start", "napcat")] = 1
        self.recreate().tick()
        self.docker.states["astrbot"] = False
        self.assertEqual(self.recreate().tick()["status"], "recovery_pending")
        self.assertNotIn("astrbot", self.ledger()["active"]["started"])
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls.count(("start", "astrbot")), 2)

    def test_unattempted_not_running_prevents_false_group_completion(self):
        self.docker.states["other"] = False
        self.docker.failures[("stop", "napcat")] = 1
        self.request(targets=["astrbot", "napcat", "other"])
        self.assertEqual(self.watchdog.tick()["status"], "recovery_pending")
        self.assertNotIn(("start", "other"), self.docker.calls)
        self.assertIsNotNone(self.ledger()["active"])
        self.docker.states["other"] = True
        self.assertEqual(self.recreate().tick()["status"], "failed")

    def test_crash_after_attempt_persisted_before_stop_returns_recovers_attempt(self):
        def crash(operation, target):
            if operation == "stop":
                self.assertEqual(self.ledger()["active"]["attempted"], ["astrbot"])
                self.assertTrue(self.config.processing.exists())
                self.assertFalse(self.config.request.exists())
                raise Crash()
        self.docker.hook = crash
        self.request()
        with self.assertRaises(Crash):
            self.watchdog.tick()
        self.docker.hook = None
        result = self.recreate().tick()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.docker.calls, [("stop", "astrbot"), ("start", "astrbot")])
        self.assertTrue(self.docker.states["astrbot"])
        self.assertIsNone(self.ledger()["active"])
        self.assertFalse(self.config.processing.exists())

    def test_crash_before_stop_command_after_attempt_save_is_safe(self):
        def persist(path, payload):
            wd.atomic_json(path, payload)
            if path == self.config.state and payload["active"] and payload["active"]["attempted"]:
                raise Crash()
        self.request()
        with self.assertRaises(Crash):
            self.recreate(persist).tick()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.recreate().tick()["status"], "failed")
        self.assertEqual(self.docker.calls, [("start", "astrbot")])

    def test_crash_after_all_stops_before_sleep_phase_recovers_entire_group(self):
        def crash(operation, target):
            if operation == "stop" and target == "napcat":
                raise Crash()
        self.docker.hook = crash
        self.request()
        with self.assertRaises(Crash):
            self.watchdog.tick()
        self.docker.hook = None
        self.assertEqual(self.recreate().tick()["status"], "failed")
        self.assertEqual(self.docker.calls[-2:], [("start", "astrbot"), ("start", "napcat")])

    def test_crash_during_start_retries_only_unconfirmed_targets(self):
        self.request()
        self.watchdog.tick()
        self.clock.now += 60

        def crash(operation, target):
            if operation == "start" and target == "napcat":
                raise Crash()
        self.docker.hook = crash
        with self.assertRaises(Crash):
            self.recreate().tick()
        self.assertEqual(self.ledger()["active"]["started"], ["astrbot"])
        self.docker.hook = None
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls.count(("start", "astrbot")), 1)
        self.assertEqual(self.docker.calls.count(("start", "napcat")), 2)

    def test_interrupted_restart_starts_attempted_without_restarting_completed(self):
        def crash(operation, target):
            if operation == "restart" and target == "napcat":
                raise Crash()
        self.docker.hook = crash
        self.request(duration=0)
        with self.assertRaises(Crash):
            self.watchdog.tick()
        self.docker.hook = None
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls, [("restart", "astrbot"), ("restart", "napcat"),
                                           ("start", "napcat")])

    def test_interrupted_restart_unattempted_target_still_restarts_once(self):
        def crash(operation, target):
            if operation == "restart" and target == "astrbot":
                raise Crash()
        self.docker.hook = crash
        self.request(duration=0)
        with self.assertRaises(Crash):
            self.watchdog.tick()
        self.docker.hook = None
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls, [("restart", "astrbot"), ("start", "astrbot"),
                                           ("restart", "napcat")])

    def test_saved_sleep_claim_after_crash_can_be_stale_without_losing_completion(self):
        def persist(path, payload):
            wd.atomic_json(path, payload)
            if path == self.config.result and payload["status"] == "sleeping":
                raise Crash()
        self.request()
        with self.assertRaises(Crash):
            self.recreate(persist).tick()
        self.clock.now += 1000
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.result()["status"], "completed")
        self.assertFalse(self.config.processing.exists())

    def test_claim_without_ledger_is_parsed_after_process_recreation(self):
        self.request(legacy=True)
        os.replace(self.config.request, self.config.processing)
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls, [("restart", "napcat")])

    def test_busy_rejection_keeps_active_and_eventual_completion_result(self):
        self.request()
        self.watchdog.tick()
        self.request(nonce="busy", legacy=True)
        result = self.recreate().tick()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["detail"], "busy")
        self.assertEqual(result["nonce"], "busy")
        self.assertEqual(self.ledger()["active"]["nonce"], "n1")
        self.assertEqual(self.docker.calls, [("stop", "astrbot"), ("stop", "napcat")])
        self.clock.now += 60
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.result()["nonce"], "n1")

    def test_busy_crash_claim_is_processed_after_active_recovery(self):
        self.request()
        self.watchdog.tick()
        self.request(nonce="busy", legacy=True)
        os.replace(self.config.request, self.config.processing)
        self.assertEqual(self.recreate().tick()["detail"], "busy")
        self.assertEqual(self.ledger()["active"]["nonce"], "n1")

    def test_new_request_written_during_execution_is_not_removed(self):
        new_request = None

        def post(operation, target):
            nonlocal new_request
            if new_request is None:
                new_request = self.request(nonce="new", legacy=True)
        self.docker.hook = post
        self.request(legacy=True)
        self.assertEqual(self.watchdog.tick()["status"], "completed")
        self.assertTrue(self.config.request.exists())
        self.assertEqual(json.loads(self.config.request.read_text())["nonce"], "new")
        self.assertFalse(self.config.processing.exists())
        self.docker.hook = None
        self.assertEqual(self.recreate().tick()["nonce"], "new")

    def test_legacy_replay_rejected_after_acceptance(self):
        self.request(legacy=True)
        self.watchdog.tick()
        self.request(legacy=True)
        result = self.recreate().tick()
        self.assertEqual(result["detail"], "replay")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.docker.calls, [("restart", "napcat")])

    def test_replay_rejected_while_sleeping(self):
        self.request()
        self.watchdog.tick()
        self.request()
        self.assertEqual(self.recreate().tick()["detail"], "replay")
        self.assertEqual(self.ledger()["active"]["nonce"], "n1")
        self.assertEqual(len(self.docker.calls), 2)

    def test_seen_retained_through_freshness_plus_future_window(self):
        self.request(legacy=True)
        self.watchdog.tick()
        self.clock.now += 179
        self.request(nonce="next", legacy=True)
        self.recreate().tick()
        self.assertIn("n1", self.ledger()["seen"])
        self.request(legacy=True)
        self.assertEqual(self.recreate().tick()["detail"], "replay")
        self.clock.now += 2
        self.request(nonce="third", legacy=True)
        self.recreate().tick()
        self.assertNotIn("n1", self.ledger()["seen"])

    def test_stale_and_future_requests_rejected(self):
        for timestamp in (self.clock() - 121, self.clock() + 61):
            with self.subTest(timestamp=timestamp):
                self.request(ts=timestamp)
                self.assertEqual(self.watchdog.tick()["detail"], "stale-request")
        self.assertEqual(self.docker.calls, [])

    def test_freshness_boundaries_are_accepted(self):
        for offset in (-120, 60):
            with self.subTest(offset=offset):
                self.request(nonce="boundary_" + str(abs(offset)), legacy=True, ts=self.clock() + offset)
                self.assertEqual(self.watchdog.tick()["status"], "completed")

    def test_timestamp_type_and_finiteness_rejected(self):
        for timestamp in (True, False, None, "10000", [], {}, 10 ** 400):
            with self.subTest(timestamp=type(timestamp).__name__):
                self.request(ts=timestamp)
                self.assertEqual(self.watchdog.tick()["status"], "rejected")
        for number in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(number=number):
                self.config.request.write_text(
                    '{"target":"napcat","nonce":"n1","ts":' + number + '}', encoding="utf-8")
                self.assertEqual(self.watchdog.tick()["status"], "rejected")
        self.assertEqual(self.docker.calls, [])

    def test_target_validation_rejects_injection_and_non_allowlisted_names(self):
        for target in ("../napcat", "napcat;exit", "--help", "napcat\n", "napcat/a",
                       "napcat\\x", "a b", "", "a" * 64, "é", "notallowed", None, 2):
            with self.subTest(target=target):
                self.request(targets=[target])
                self.assertEqual(self.watchdog.tick()["detail"], "target-not-allowed")
        self.assertEqual(self.docker.calls, [])

    def test_target_regex_boundaries(self):
        for name in ("a", "A0_-.name", "a" * 63):
            self.assertIsNotNone(wd.TARGET.fullmatch(name))
        for name in ("a" * 64, ".name", "-name", "_name", "a\n"):
            self.assertIsNone(wd.TARGET.fullmatch(name))

    def test_group_validation_and_size_limits(self):
        for targets in ([], ["astrbot", "astrbot"], "astrbot", None, {}, [None]):
            with self.subTest(targets=targets):
                self.request(targets=["astrbot"])
                payload = json.loads(self.config.request.read_text())
                payload["targets"] = targets
                wd.atomic_json(self.config.request, payload)
                self.assertEqual(self.watchdog.tick()["status"], "rejected")
        eleven = ["t" + str(i) for i in range(11)]
        self.config.allowed.update(eleven)
        self.request(targets=eleven)
        self.assertEqual(self.watchdog.tick()["status"], "rejected")
        self.request(targets=eleven[:10], duration=0)
        self.assertEqual(self.watchdog.tick()["status"], "completed")
        self.assertEqual(len(self.docker.calls), 10)

    def test_nonce_validation_rejects_empty_long_and_injected_values(self):
        for nonce in ("", "a" * 129, "../x", "a b", "a\n", "é", None, 1, True):
            with self.subTest(nonce=nonce):
                self.request(nonce=nonce)
                self.assertEqual(self.watchdog.tick()["detail"], "invalid-nonce")
        self.assertEqual(self.docker.calls, [])
        self.request(nonce="A_9-" + "x" * 124, legacy=True)
        self.assertEqual(self.watchdog.tick()["status"], "completed")

    def test_duration_must_be_integer_nonbool_in_range(self):
        for duration in (True, False, -1, 121, 1.0, "1", None, [], {}):
            with self.subTest(duration=duration):
                self.request(duration=duration)
                self.assertEqual(self.watchdog.tick()["detail"], "invalid-operation-or-duration")
        self.assertEqual(self.docker.calls, [])
        self.request(duration=120)
        self.assertEqual(self.watchdog.tick()["wake_at"], self.clock() + 7200)

    def test_invalid_version_operation_reason_and_missing_fields(self):
        payload = self.request()
        invalid = []
        for version in (True, 2.0, 1, 3, None, "2"):
            invalid.append(dict(payload, version=version))
        for operation in ("stop", "restart", None, []):
            invalid.append(dict(payload, operation=operation))
        invalid.append(dict(payload, reason={}))
        for key in ("nonce", "ts", "duration_min", "targets", "operation"):
            missing = dict(payload)
            del missing[key]
            invalid.append(missing)
        for item in invalid:
            with self.subTest(item=item):
                wd.atomic_json(self.config.request, item)
                self.assertEqual(self.watchdog.tick()["status"], "rejected")
        self.assertEqual(self.docker.calls, [])

    def test_malformed_json_and_nonobject_payloads_are_consumed(self):
        for text in ("{bad json", "", "[]", "null", "12", '"text"'):
            with self.subTest(text=text):
                self.config.request.write_text(text, encoding="utf-8")
                self.assertEqual(self.watchdog.tick()["status"], "rejected")
                self.assertFalse(self.config.request.exists())
                self.assertFalse(self.config.processing.exists())
        self.assertEqual(self.docker.calls, [])

    def test_corrupt_ledger_refuses_commands_and_preserves_request(self):
        for text in ("{bad", "[]", "{}", '{"active":null,"seen":[]}',
                     '{"active":null,"seen":{"n1":NaN}}'):
            with self.subTest(text=text):
                self.config.state.write_text(text, encoding="utf-8")
                self.request(legacy=True)
                self.assertEqual(self.recreate().tick()["status"], "rejected")
                self.assertEqual(self.docker.calls, [])
                self.assertTrue(self.config.request.exists())
                self.assertEqual(self.config.state.read_text(), text)

    def test_corrupt_active_ledger_refuses_commands(self):
        self.request()
        self.watchdog.tick()
        valid = self.ledger()
        calls = list(self.docker.calls)
        cases = {"targets": ["../escape"], "phase": "bogus", "attempted": ["other"],
                 "started": ["other"], "wake_at": "nan", "duration_min": True,
                 "version": True, "operation": "restart", "failed": 1}
        for field, value in cases.items():
            with self.subTest(field=field):
                bad = copy.deepcopy(valid)
                bad["active"][field] = value
                wd.atomic_json(self.config.state, bad)
                self.recreate().tick()
                self.assertEqual(self.docker.calls, calls)
                self.assertEqual(self.ledger(), bad)

    def test_unwritable_ledger_refuses_all_commands_and_keeps_claim(self):
        def persist(path, payload):
            if path == self.config.state:
                raise PermissionError("test read-only ledger")
            wd.atomic_json(path, payload)
        self.request()
        result = self.recreate(persist).tick()
        self.assertEqual(result["status"], "recovery_pending")
        self.assertEqual(self.docker.calls, [])
        self.assertTrue(self.config.processing.exists())
        self.assertFalse(self.config.state.exists())
        self.assertEqual(self.recreate().tick()["status"], "sleeping")

    def test_unwritable_attempt_save_never_executes_unsaved_stop(self):
        def persist(path, payload):
            if path == self.config.state and payload["active"] and payload["active"]["attempted"]:
                raise PermissionError("test attempt persist failure")
            wd.atomic_json(path, payload)
        self.request()
        self.recreate(persist).tick()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.ledger()["active"]["attempted"], [])
        self.assertEqual(self.recreate().tick()["status"], "failed")
        self.assertEqual(self.docker.calls, [])

    def test_wake_phase_persistence_failure_prevents_start_until_later(self):
        self.request()
        self.watchdog.tick()
        self.clock.now += 60

        def persist(path, payload):
            if path == self.config.state:
                raise PermissionError("test starting persist failure")
            wd.atomic_json(path, payload)
        self.assertEqual(self.recreate(persist).tick()["status"], "recovery_pending")
        self.assertEqual(self.docker.calls, [("stop", "astrbot"), ("stop", "napcat")])
        self.assertEqual(self.ledger()["active"]["phase"], "sleeping")
        self.assertEqual(self.recreate().tick()["status"], "completed")

    def test_failed_completion_result_write_preserves_active_obligation(self):
        def persist(path, payload):
            if path == self.config.result and payload["status"] == "completed":
                raise PermissionError("test result write failure")
            wd.atomic_json(path, payload)
        self.request(legacy=True)
        self.recreate(persist).tick()
        self.assertIsNotNone(self.ledger()["active"])
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls, [("restart", "napcat")])

    def test_failed_terminal_ledger_clear_retains_obligation_and_claim_is_retired(self):
        def persist(path, payload):
            if path == self.config.state and payload["active"] is None:
                raise PermissionError("test terminal clear failure")
            wd.atomic_json(path, payload)
        self.request(legacy=True)
        result = self.recreate(persist).tick()
        self.assertEqual(result["status"], "recovery_pending")
        self.assertIsNotNone(self.ledger()["active"])
        self.assertFalse(self.config.processing.exists())
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls, [("restart", "napcat")])

    def test_restart_health_pending_retries_without_marking_command_failed(self):
        running = self.docker.running
        self.docker.running = lambda target: False
        self.request(legacy=True)
        self.assertEqual(self.watchdog.tick()["status"], "recovery_pending")
        self.assertFalse(self.ledger()["active"]["failed"])
        self.docker.running = running
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertEqual(self.docker.calls.count(("restart", "napcat")), 1)

    def test_lock_acquisition_error_preserves_request_and_existing_result(self):
        self.request(legacy=True)
        wd.atomic_json(self.config.result, {"sentinel": True})
        with patch.object(wd.AdvisoryLock, "__enter__", side_effect=PermissionError("lock denied")):
            self.assertIsNone(self.watchdog.tick())
        self.assertEqual(self.result(), {"sentinel": True})
        self.assertTrue(self.config.request.exists())
        self.assertEqual(self.docker.calls, [])

    def test_persistence_error_result_is_written_before_lock_release(self):
        def persist(path, payload):
            if path == self.config.state:
                raise PermissionError("ledger denied")
            if path == self.config.result:
                with wd.AdvisoryLock(self.config.lock) as acquired:
                    self.assertFalse(acquired)
            wd.atomic_json(path, payload)
        self.request()
        self.recreate(persist).tick()
        self.assertEqual(self.docker.calls, [])

    def test_shell_wrappers_and_systemd_paths_are_coherent(self):
        wrapper = (ROOT / "deploy" / "guardian-watchdog.sh").read_text(encoding="utf-8")
        tests = (ROOT / "tests" / "test_watchdog.sh").read_text(encoding="utf-8")
        service = (ROOT / "deploy" / "guardian-watchdog.service").read_text(encoding="utf-8")
        timer = (ROOT / "deploy" / "guardian-watchdog.timer").read_text(encoding="utf-8")
        self.assertIn('${GUARDIAN_PYTHON:-${PYTHON:-python3}}', wrapper)
        self.assertIn('"$HERE/guardian-watchdog.py" "$@"', wrapper)
        self.assertIn('/usr/local/bin/guardian-watchdog.py', wrapper)
        self.assertIn('${PYTHON:-python3}', tests)
        self.assertIn('"$HERE/test_watchdog.py"', tests)
        self.assertIn('Type=oneshot', service)
        self.assertIn('Environment="GUARDIAN_ALLOWED_TARGETS=astrbot napcat"', service)
        self.assertIn('ExecStart=/usr/local/bin/guardian-watchdog.sh', service)
        self.assertIn('OnUnitActiveSec=30s', timer)

    def test_concurrent_lock_is_nonblocking_and_leaves_file_in_place(self):
        self.request(legacy=True)
        with wd.AdvisoryLock(self.config.lock) as acquired:
            self.assertTrue(acquired)
            self.assertIsNone(self.recreate().tick())
            self.assertEqual(self.docker.calls, [])
            self.assertTrue(self.config.request.exists())
        self.assertTrue(self.config.lock.exists())
        self.assertEqual(self.recreate().tick()["status"], "completed")
        self.assertTrue(self.config.lock.exists())

    def test_os_releases_lock_on_process_death(self):
        # The child performs only file locking; it never invokes Docker.
        code = (
            "import importlib.util,sys;"
            "s=importlib.util.spec_from_file_location('wd',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "lock=m.AdvisoryLock(sys.argv[2]);"
            "assert lock.__enter__();sys.stdout.write('locked\\n');sys.stdout.flush();"
            "sys.stdin.read(1)"
        )
        process = subprocess.Popen([sys.executable, "-B", "-c", code,
                                    str(ROOT / "deploy" / "guardian-watchdog.py"), str(self.config.lock)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, shell=False)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with wd.AdvisoryLock(self.config.lock) as acquired:
                self.assertFalse(acquired)
            process.communicate("x", timeout=10)
            self.assertEqual(process.returncode, 0)
            with wd.AdvisoryLock(self.config.lock) as acquired:
                self.assertTrue(acquired)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
            else:
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()

    def test_no_request_is_silent_and_runs_no_commands(self):
        self.assertIsNone(self.watchdog.tick())
        self.assertEqual(self.docker.calls, [])
        self.assertFalse(self.config.result.exists())

    def test_paths_configuration_and_backend_validation(self):
        for key, value in (("GUARDIAN_DOCKER_BACKEND", "unknown"),
                           ("GUARDIAN_CMD_TIMEOUT", "0"), ("GUARDIAN_CMD_TIMEOUT", "nan"),
                           ("GUARDIAN_MAX_AGE_SEC", "-1"), ("GUARDIAN_MAX_AGE_SEC", "inf"),
                           ("GUARDIAN_DOCKER_PATH", "")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                wd.Config({key: value})
        self.assertEqual(wd.Config({"GUARDIAN_MAX_AGE_SEC": "999"}).max_age, 120)
        for conflict in (self.config.request, self.config.processing, self.config.state, self.config.lock):
            with self.subTest(conflict=str(conflict)), self.assertRaises(ValueError):
                wd.Config({"GUARDIAN_REQUEST_FILE": str(self.config.request),
                           "GUARDIAN_RESULT_FILE": str(conflict)})
        with self.assertRaises(ValueError):
            wd.Config({"GUARDIAN_REQUEST_FILE": str(self.directory / "watchdog_state.json")})

    def test_atomic_json_fsync_replace_and_no_partial_file(self):
        path = self.directory / "atomic.json"
        wd.atomic_json(path, {"old": True})
        with patch.object(wd.os, "fsync", wraps=wd.os.fsync) as fsync:
            wd.atomic_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"new": True})
        self.assertGreaterEqual(fsync.call_count, 1 if os.name == "nt" else 2)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        with patch.object(wd.os, "replace", side_effect=PermissionError("test replace failure")):
            with self.assertRaises(PermissionError):
                wd.atomic_json(path, {"partial": True})
        self.assertEqual(json.loads(path.read_text()), {"new": True})
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_subprocess_compose_commands_are_lists_with_timeout_and_no_shell(self):
        self.config.docker = "C:/Docker With Spaces/docker.exe"
        calls = []

        def runner(arguments, **options):
            calls.append((arguments, options))
            if "ps" in arguments:
                output = "abc123\ndef456\n"
            elif "inspect" in arguments:
                output = json.dumps([{"State": {"Running": True}},
                                     {"State": {"Running": True, "Health": {"Status": "healthy"}}}])
            else:
                output = ""
            return subprocess.CompletedProcess(arguments, 0, output, "")
        docker = wd.Docker(self.config, runner)
        for operation in ("stop", "start", "restart"):
            self.assertTrue(docker.action(operation, "napcat"))
        self.assertTrue(docker.running("napcat"))
        prefix = [self.config.docker, "compose", "--project-directory", self.config.compose_dir]
        self.assertEqual([item[0] for item in calls],
                         [prefix + [operation, "napcat"] for operation in ("stop", "start", "restart")] +
                         [prefix + ["ps", "-q", "napcat"], [self.config.docker, "inspect", "abc123", "def456"]])
        for arguments, options in calls:
            self.assertIsInstance(arguments, list)
            self.assertIs(options["shell"], False)
            self.assertEqual(options["timeout"], 120)

    def test_subprocess_container_backend_uses_actual_container_names(self):
        self.config.backend = "container"
        calls = []

        def runner(arguments, **options):
            calls.append(arguments)
            output = '[{"State":{"Running":true}}]' if "inspect" in arguments else ""
            return subprocess.CompletedProcess(arguments, 0, output, "")
        docker = wd.Docker(self.config, runner)
        for operation in ("stop", "start", "restart"):
            self.assertTrue(docker.action(operation, "local.napcat-1"))
        self.assertTrue(docker.running("local.napcat-1"))
        self.assertEqual(calls, [["docker", operation, "local.napcat-1"]
                                 for operation in ("stop", "start", "restart", "inspect")])

    def test_inspect_rejects_missing_stopped_unhealthy_and_malformed_state(self):
        self.config.backend = "container"
        for output in ("bad", "[]", "{}", "[{}]", '[{"State":{"Running":false}}]',
                       '[{"State":{"Running":1}}]', '[{"State":{"Running":true,"Health":{"Status":"starting"}}}]',
                       '[{"State":{"Running":true,"Health":{"Status":"unhealthy"}}}]'):
            with self.subTest(output=output):
                runner = lambda arguments, **options: subprocess.CompletedProcess(arguments, 0, output, "")
                self.assertFalse(wd.Docker(self.config, runner).running("napcat"))

    def test_compose_resolution_rejects_empty_and_injected_identifiers(self):
        for output in ("", "--help", "../escape", "abc;exit", "a" * 128 + "x"):
            with self.subTest(output=output):
                calls = []

                def runner(arguments, **options):
                    calls.append(arguments)
                    return subprocess.CompletedProcess(arguments, 0, output, "")
                self.assertFalse(wd.Docker(self.config, runner).running("napcat"))
                self.assertEqual(len(calls), 1)

    def test_subprocess_failure_timeout_and_missing_executable_are_safe(self):
        for failure in (subprocess.TimeoutExpired(["docker"], 120), FileNotFoundError("docker")):
            with self.subTest(failure=type(failure).__name__):
                runner = unittest.mock.Mock(side_effect=failure)
                docker = wd.Docker(self.config, runner)
                self.assertFalse(docker.action("stop", "napcat"))
                self.assertFalse(docker.running("napcat"))
        runner = lambda arguments, **options: subprocess.CompletedProcess(arguments, 1, "", "failed")
        self.assertFalse(wd.Docker(self.config, runner).action("restart", "napcat"))

    def test_cli_defaults_once_and_optional_foreground_polling(self):
        fake = unittest.mock.Mock()
        with patch.object(wd, "Config", return_value=self.config), patch.object(wd, "Watchdog", return_value=fake):
            self.assertEqual(wd.main([]), 0)
            self.assertEqual(fake.tick.call_count, 1)
            fake.reset_mock()
            self.assertEqual(wd.main(["--once"]), 0)
            self.assertEqual(fake.tick.call_count, 1)
            fake.reset_mock()
            with patch.object(wd.time, "sleep", side_effect=[None, KeyboardInterrupt()]) as sleep:
                self.assertEqual(wd.main(["--poll-interval", "30"]), 0)
            self.assertEqual(fake.tick.call_count, 2)
            self.assertEqual(sleep.call_args_list, [unittest.mock.call(30), unittest.mock.call(30)])

    def test_cli_rejects_invalid_poll_intervals(self):
        for interval in ("0", "-1", "nan", "inf"):
            with self.subTest(interval=interval), patch.object(sys, "stderr"), self.assertRaises(SystemExit):
                wd.main(["--poll-interval", interval])


if __name__ == "__main__":
    unittest.main(verbosity=2)
