#!/usr/bin/env python3
"""Host-owned Docker restart/sleep watchdog (stdlib; Linux and Windows).

Linux: install beside guardian-watchdog.sh and run the systemd oneshot timer.
Windows: run python guardian-watchdog.py --poll-interval 30 in the foreground.
The default is one tick; sleeping jobs never block a oneshot invocation.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


TARGET = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}\Z", re.ASCII)
NONCE = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z", re.ASCII)
CONTAINER_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z", re.ASCII)


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def strict_json(text):
    def invalid_constant(value):
        raise ValueError("non-finite JSON number: " + value)
    return json.loads(text, parse_constant=invalid_constant)


def atomic_json(path, payload):
    """Persist in the same directory, including its rename on Linux."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class AdvisoryLock:
    """Nonblocking OS lock; never unlink the lock file/inode."""

    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.stream = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0, os.SEEK_END)
                if self.stream.tell() == 0:
                    self.stream.write(b"\0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            return False
        return True

    def __exit__(self, *unused):
        if self.stream is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.compose_dir = env.get("GUARDIAN_COMPOSE_DIR", "/opt/astrbot-stack")
        data_dir = Path(self.compose_dir) / "data" / "guardian"
        self.request = Path(env.get("GUARDIAN_REQUEST_FILE", str(data_dir / "restart_request.json")))
        self.result = Path(env.get("GUARDIAN_RESULT_FILE", str(data_dir / "restart_result.json")))
        self.log = Path(env.get("GUARDIAN_LOG_FILE", "/var/log/guardian-watchdog.log"))
        self.allowed = set(env.get("GUARDIAN_ALLOWED_TARGETS", "astrbot napcat").split())
        self.max_age = float(env.get("GUARDIAN_MAX_AGE_SEC", "120"))
        self.timeout = float(env.get("GUARDIAN_CMD_TIMEOUT", "120"))
        self.backend = env.get("GUARDIAN_DOCKER_BACKEND", "compose")
        self.docker = env.get("GUARDIAN_DOCKER_PATH", "docker")
        if (not math.isfinite(self.max_age) or self.max_age < 0 or
                not math.isfinite(self.timeout) or self.timeout <= 0 or
                self.backend not in ("compose", "container") or not self.docker):
            raise ValueError("invalid watchdog configuration")
        self.max_age = min(self.max_age, 120)
        self.state = self.request.parent / "watchdog_state.json"
        self.lock = self.request.parent / "watchdog_state.lock"
        self.processing = Path(str(self.request) + ".processing")
        paths = [path.resolve() for path in
                 (self.request, self.processing, self.result, self.state, self.lock, self.log)]
        if len(set(paths)) != len(paths):
            raise ValueError("watchdog input, output, state and log paths must be distinct")


class Docker:
    def __init__(self, config, runner=subprocess.run, log=lambda message: None):
        self.config = config
        self.runner = runner
        self.log = log

    def command(self, arguments):
        try:
            result = self.runner([self.config.docker] + arguments, shell=False,
                                 timeout=self.config.timeout, capture_output=True,
                                 text=True)
            if result.returncode != 0:
                self.log("Docker command failed: " + str(result.stderr))
                return None
            return result.stdout
        except (OSError, subprocess.SubprocessError) as error:
            self.log("Docker command failed: " + str(error))
            return None

    def compose(self, arguments):
        return ["compose", "--project-directory", self.config.compose_dir] + arguments

    def action(self, operation, target):
        arguments = [operation, target]
        if self.config.backend == "compose":
            arguments = self.compose(arguments)
        return self.command(arguments) is not None

    def running(self, target):
        if self.config.backend == "compose":
            output = self.command(self.compose(["ps", "-q", target]))
            if output is None:
                return False
            identifiers = output.split()
        else:
            identifiers = [target]
        if not identifiers or any(not CONTAINER_ID.fullmatch(item) for item in identifiers):
            return False
        output = self.command(["inspect"] + identifiers)
        if output is None:
            return False
        try:
            containers = strict_json(output)
            if not isinstance(containers, list) or len(containers) != len(identifiers):
                return False
            for container in containers:
                state = container["State"]
                if state.get("Running") is not True:
                    return False
                if "Health" in state and state["Health"].get("Status") != "healthy":
                    return False
            return True
        except (ValueError, KeyError, TypeError, AttributeError):
            return False


class Watchdog:
    def __init__(self, config, docker=None, clock=time.time, persist=atomic_json):
        self.config = config
        self.clock = clock
        self.persist = persist
        self.docker = docker if docker is not None else Docker(config, log=self.log)
        self.ledger = None

    def log(self, message):
        try:
            with self.config.log.open("a", encoding="utf-8") as stream:
                stream.write("[{}] {}\n".format(self.clock(), message.replace("\n", " ")))
        except OSError:
            pass

    def targets_valid(self, targets):
        return (isinstance(targets, list) and 1 <= len(targets) <= 10 and
                all(isinstance(item, str) and TARGET.fullmatch(item) and
                    item in self.config.allowed for item in targets) and
                len(set(targets)) == len(targets))

    def validate_request(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("invalid-request")
        if "version" not in payload:
            targets = [payload.get("target")]
            operation, duration, version = "restart", 0, 1
        elif type(payload["version"]) is int and payload["version"] == 2:
            targets = payload.get("targets")
            duration = payload.get("duration_min")
            if (payload.get("operation") != "sleep" or type(duration) is not int or
                    not 0 <= duration <= 120):
                raise ValueError("invalid-operation-or-duration")
            operation, version = "sleep", 2
        else:
            raise ValueError("invalid-version")
        if not self.targets_valid(targets):
            raise ValueError("target-not-allowed")
        nonce, timestamp = payload.get("nonce"), payload.get("ts")
        if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
            raise ValueError("invalid-nonce")
        if not finite_number(timestamp):
            raise ValueError("invalid-timestamp")
        age = self.clock() - timestamp
        if age > self.config.max_age or age < -60:
            raise ValueError("stale-request")
        reason = payload.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError("invalid-reason")
        return {"version": version, "operation": operation, "targets": targets,
                "duration_min": duration, "nonce": nonce, "ts": timestamp,
                "reason": reason}

    def load_ledger(self):
        try:
            ledger = strict_json(self.config.state.read_text(encoding="utf-8"))
        except FileNotFoundError:
            ledger = {"active": None, "seen": {}}
        if (not isinstance(ledger, dict) or set(ledger) != {"active", "seen"} or
                not isinstance(ledger["seen"], dict)):
            raise ValueError("invalid-ledger")
        for nonce, timestamp in ledger["seen"].items():
            if not NONCE.fullmatch(nonce) or not finite_number(timestamp):
                raise ValueError("invalid-ledger")
        job = ledger["active"]
        if job is not None:
            try:
                if (not isinstance(job, dict) or not self.targets_valid(job["targets"]) or
                        not isinstance(job["nonce"], str) or not NONCE.fullmatch(job["nonce"]) or
                        job["nonce"] not in ledger["seen"] or
                        not finite_number(job["ts"]) or not finite_number(job["wake_at"]) or
                        type(job["version"]) is not int or job["version"] not in (1, 2) or
                        job["operation"] not in ("sleep", "restart") or
                        (job["version"] == 1 and (job["operation"] != "restart" or
                                                 len(job["targets"]) != 1)) or
                        (job["version"] == 2 and job["operation"] != "sleep") or
                        type(job["duration_min"]) is not int or not 0 <= job["duration_min"] <= 120 or
                        (job["operation"] == "restart" and job["duration_min"] != 0) or
                        job["phase"] not in ("stopping", "sleeping", "starting", "restarting") or
                        (job["phase"] in ("stopping", "sleeping") and job["duration_min"] == 0) or
                        (job["phase"] == "restarting" and job["duration_min"] != 0) or
                        type(job["failed"]) is not bool or not isinstance(job["detail"], str) or
                        not isinstance(job["reason"], str)):
                    raise ValueError("invalid-ledger")
                for field in ("attempted", "started"):
                    items = job[field]
                    if (not isinstance(items, list) or any(item not in job["targets"] for item in items) or
                            len(set(items)) != len(items)):
                        raise ValueError("invalid-ledger")
                if (not set(job["started"]).issubset(job["attempted"]) or
                        job["attempted"] != job["targets"][:len(job["attempted"])] or
                        (job["phase"] in ("stopping", "sleeping") and job["started"]) or
                        (job["phase"] == "sleeping" and job["attempted"] != job["targets"]) or
                        (job["phase"] == "starting" and job["duration_min"] == 0 and
                         job["attempted"] != job["targets"])):
                    raise ValueError("invalid-ledger")
            except (KeyError, TypeError):
                raise ValueError("invalid-ledger") from None
        self.ledger = ledger

    def save(self):
        # Acceptance timestamps cover the whole freshness + future-skew window.
        cutoff = self.clock() - (self.config.max_age + 60)
        active = self.ledger["active"]
        self.ledger["seen"] = {
            nonce: timestamp for nonce, timestamp in self.ledger["seen"].items()
            if timestamp >= cutoff or (active and nonce == active["nonce"])
        }
        self.persist(self.config.state, self.ledger)

    def result(self, payload, status, detail):
        payload = payload if isinstance(payload, dict) else {}
        targets = payload.get("targets", [payload.get("target")])
        if not isinstance(targets, list):
            targets = []
        targets = [item for item in targets if isinstance(item, str)]
        result = {"nonce": payload.get("nonce"), "ts": self.clock(),
                  "ok": status == "completed", "status": status,
                  "targets": targets, "target": targets[0] if targets else None,
                  "detail": detail, "wake_at": payload.get("wake_at"),
                  "operation": payload.get("operation", "restart")}
        # Invalid requests may themselves contain NaN/Infinity; never echo them.
        if not isinstance(result["nonce"], str):
            result["nonce"] = None
        if not finite_number(result["wake_at"]):
            result["wake_at"] = None
        if not isinstance(result["operation"], str):
            result["operation"] = None
        self.persist(self.config.result, result)
        return result

    def finish(self, job):
        status = "failed" if job["failed"] else "completed"
        detail = job["detail"] if job["failed"] else (
            "restarted" if job["duration_min"] == 0 else "awakened")
        # Keep the obligation until its terminal result is durable, too.
        result = self.result(job, status, detail)
        # Retire the accepted claim while its obligation is still in the ledger.
        # A crash before clearing active will reverify and republish completion.
        if self.config.processing.exists():
            try:
                claim = strict_json(self.config.processing.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                claim = None
            if isinstance(claim, dict) and claim.get("nonce") == job["nonce"]:
                self.config.processing.unlink()
        # Also retain long-running jobs' crash claims after terminal persistence.
        self.ledger["seen"][job["nonce"]] = self.clock()
        self.ledger["active"] = None
        try:
            self.save()
        except (OSError, ValueError):
            self.ledger["active"] = job
            raise
        return result

    def recover(self, job):
        job["phase"] = "starting"
        self.save()
        failed = []
        for target in job["attempted"]:
            if target in job["started"]:
                continue
            if self.docker.action("start", target) and self.docker.running(target):
                job["started"].append(target)
                self.save()
            else:
                failed.append(target)
        # Recheck all intended targets before completion, including earlier flags.
        for target in job["targets"]:
            if target not in failed and not self.docker.running(target):
                failed.append(target)
                if target in job["started"]:
                    job["started"].remove(target)
                    self.save()
        if failed:
            return self.result(job, "recovery_pending", "start-or-verify-failed:" + ",".join(failed))
        return self.finish(job)

    def restart(self, job):
        for target in job["targets"]:
            if target in job["started"]:
                continue
            if target in job["attempted"]:
                # An interrupted restart might have stopped the container already.
                succeeded = self.docker.action("start", target)
            else:
                job["attempted"].append(target)
                self.save()
                succeeded = self.docker.action("restart", target)
            if not succeeded:
                job["failed"] = True
                job["detail"] = "restart-failed:" + target
                self.save()
            elif self.docker.running(target):
                job["started"].append(target)
                self.save()
        return self.recover(job)

    def stop(self, job):
        for target in job["targets"]:
            job["attempted"].append(target)
            self.save()
            if not self.docker.action("stop", target):
                job["failed"] = True
                job["detail"] = "stop-failed:" + target
                self.save()
                return self.recover(job)
        job["phase"] = "sleeping"
        job["wake_at"] = self.clock() + job["duration_min"] * 60
        self.save()
        return self.result(job, "sleeping", "stopped")

    def resume(self, job):
        if job["phase"] == "stopping":
            job["failed"] = True
            job["detail"] = "interrupted-stop"
            return self.recover(job)
        if job["phase"] == "sleeping":
            if self.clock() < job["wake_at"]:
                return self.result(job, "sleeping", "stopped")
            return self.recover(job)
        if job["phase"] == "restarting":
            return self.restart(job)
        return self.recover(job)

    def consume(self, previous=False):
        try:
            payload = strict_json(self.config.processing.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            payload = None
        if (previous and isinstance(payload, dict) and
                isinstance(payload.get("nonce"), str) and
                payload["nonce"] in self.ledger["seen"]):
            # An accepted crash claim may be stale now. Do not overwrite recovery.
            self.config.processing.unlink()
            return None
        try:
            job = self.validate_request(payload)
        except ValueError as error:
            result = self.result(payload, "rejected", str(error))
        else:
            if job["nonce"] in self.ledger["seen"]:
                result = self.result(job, "rejected", "replay")
            elif self.ledger["active"] is not None:
                result = self.result(job, "rejected", "busy")
            else:
                job.update(phase="stopping" if job["duration_min"] > 0 else "restarting",
                           wake_at=self.clock() + job["duration_min"] * 60,
                           attempted=[], started=[], failed=False, detail="")
                self.ledger["seen"][job["nonce"]] = self.clock()
                self.ledger["active"] = job
                self.save()
                self.log("Accepted {} for {}".format(job["nonce"], ",".join(job["targets"])))
                result = self.stop(job) if job["duration_min"] > 0 else self.restart(job)
        # Only remove the claimed file. A writer may already have posted another request.
        # finish() may have retired this claim before clearing the obligation.
        self.config.processing.unlink(missing_ok=True)
        return result

    def tick(self):
        self.ledger = None
        try:
            with AdvisoryLock(self.config.lock) as acquired:
                if not acquired:
                    return None
                try:
                    self.load_ledger()
                    result = None
                    if self.ledger["active"] is not None:
                        result = self.resume(self.ledger["active"])
                    if self.config.processing.exists():
                        consumed = self.consume(previous=True)
                        if consumed is not None:
                            result = consumed
                    if self.config.request.exists():
                        os.replace(self.config.request, self.config.processing)
                        result = self.consume()
                    return result
                except (OSError, ValueError, UnicodeError) as error:
                    self.log("Watchdog persistence/input error: " + str(error))
                    job = self.ledger["active"] if self.ledger is not None else None
                    try:
                        return self.result(job, "recovery_pending" if job else "rejected",
                                           "ledger-or-io-error:" + str(error))
                    except (OSError, ValueError):
                        return None
        except OSError as error:
            # Do not race another worker's result if lock acquisition itself fails.
            self.log("Watchdog lock error: " + str(error))
            return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one tick (default)")
    mode.add_argument("--poll-interval", type=float, help="foreground polling interval in seconds")
    arguments = parser.parse_args(argv)
    if arguments.poll_interval is not None and (
            not math.isfinite(arguments.poll_interval) or arguments.poll_interval <= 0):
        parser.error("--poll-interval must be finite and positive")
    try:
        watchdog = Watchdog(Config())
    except ValueError as error:
        parser.error(str(error))
    try:
        while True:
            watchdog.tick()
            if arguments.poll_interval is None:
                return 0
            time.sleep(arguments.poll_interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
