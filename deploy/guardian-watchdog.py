#!/usr/bin/env python3
"""Host-owned Docker restart/sleep watchdog (stdlib; Linux and Windows).

Install it with the bundled installer, which discovers the real compose project
directory and container names, then writes `guardian-watchdog.env`:

  Linux    sudo ./install-watchdog.sh            (systemd timer, every 30s)
  Windows  powershell -ExecutionPolicy Bypass -File .\\install-watchdog.ps1
                                                 (scheduled task, every 30s)

Manual run: `python guardian-watchdog.py --poll-interval 30` in the foreground.
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


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def default_data_root(compose_dir):
    """宿主机侧 `./data` 目录。

    插件（容器内）写的是 `/AstrBot/data/guardian/...`，而 compose 示例把
    `./data` 挂到 `/AstrBot/data`（见 deploy/docker-compose.yml）。所以宿主侧
    的落点就是 `<compose_dir>/data/guardian/`。历史上这里少写了这一段，导致
    请求文件写出去无人消费，且**失败是静默的**。安装脚本会显式传
    `GUARDIAN_DATA_ROOT` 覆盖，默认值只作兜底。
    """
    return Path(compose_dir) / "data"


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.compose_dir = env.get("GUARDIAN_COMPOSE_DIR", "/opt/astrbot-stack")
        self.data_root = Path(env.get("GUARDIAN_DATA_ROOT") or default_data_root(self.compose_dir))
        data_dir = self.data_root / "guardian"
        self.request = Path(env.get("GUARDIAN_REQUEST_FILE", str(data_dir / "restart_request.json")))
        self.result = Path(env.get("GUARDIAN_RESULT_FILE", str(data_dir / "restart_result.json")))
        self.log = Path(env.get("GUARDIAN_LOG_FILE", "/var/log/guardian-watchdog.log"))
        # 容器名由安装脚本按 `docker compose ps` 的真实结果写入；默认值只是
        # compose 示例里的名字，不能假设用户一定照抄。
        allowed = env.get("GUARDIAN_ALLOWED_TARGETS", "astrbot napcat")
        self.allowed = {item for item in re.split(r"[\s,]+", allowed) if item}
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

        # ---- 域 C 的内核心跳守护（B16 §4 / §9 M4 / §12.3.5） ------------ #
        # 内核无法可靠地重启自己，所以"内核卡死 → 重启内核容器"必须由宿主看门狗
        # 负责。两条触发源（心跳超时 / 适配层重载请求）共用**同一条重启路径**。
        self.kernel_guard = truthy(env.get("GUARDIAN_KERNEL_GUARD", "1"))
        self.kernel_container = env.get("GUARDIAN_KERNEL_CONTAINER", "guardian-kernel").strip()
        self.kernel_heartbeat = Path(env.get(
            "GUARDIAN_KERNEL_HEARTBEAT_FILE", str(data_dir / "kernel_heartbeat.json")))
        self.kernel_reload_request = Path(env.get(
            "GUARDIAN_KERNEL_RELOAD_REQUEST_FILE",
            str(data_dir / "kernel_reload_request.json")))
        self.kernel_reload_processing = Path(str(self.kernel_reload_request) + ".processing")
        self.kernel_state = Path(env.get(
            "GUARDIAN_KERNEL_STATE_FILE", str(data_dir / "watchdog_kernel_state.json")))
        self.kernel_lock = Path(str(self.kernel_state) + ".lock")
        self.kernel_timeout = float(env.get("GUARDIAN_KERNEL_HEARTBEAT_TIMEOUT", "90"))
        self.kernel_cooldown = float(env.get("GUARDIAN_KERNEL_RESTART_COOLDOWN", "300"))
        if (not math.isfinite(self.kernel_timeout) or self.kernel_timeout <= 0 or
                not math.isfinite(self.kernel_cooldown) or self.kernel_cooldown < 0):
            raise ValueError("invalid watchdog kernel-guard configuration")
        if self.kernel_guard and not self.kernel_container:
            raise ValueError("kernel container name must not be empty when the guard is enabled")
        if self.kernel_container and not TARGET.fullmatch(self.kernel_container):
            raise ValueError("invalid kernel container name")

        paths = [path.resolve() for path in
                 (self.request, self.processing, self.result, self.state, self.lock, self.log,
                  self.kernel_heartbeat, self.kernel_reload_request,
                  self.kernel_reload_processing, self.kernel_state, self.kernel_lock)]
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

    def kernel_restart(self, container):
        """重启内核容器（B16 §4：容器无法可靠地重启自己，必须由宿主执行）。"""
        arguments = ["restart", container]
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


class KernelGuard:
    """域 C 的内核心跳守护 + 适配层重载请求消费（B16 §4 / §9 M4 / §12.3.5）。

    **为什么这条必须留在宿主**：容器无法可靠地重启自己——自杀式重启后不保证能
    回来。所以"内核卡死 → 重启内核容器"只能由看门狗做（B16 §4）。

    两条触发源（① 内核心跳超时 ② 适配层写下的 reload 请求）共用**同一条重启
    路径**：同一把 flock、同一套 nonce 去重、同一个冷却计时。这样"心跳守护"与
    "适配层请求"不会并发打架（B16 §4 的关键结论）。
    """

    def __init__(self, config, docker, clock=time.time, persist=atomic_json,
                 log=lambda message: None):
        self.config = config
        self.docker = docker
        self.clock = clock
        self.persist = persist
        self.log = log
        self.state = None

    # ------------------------------------------------------------------ #
    def load_state(self):
        try:
            state = strict_json(self.config.kernel_state.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"seen": {}, "last_restart_ts": 0.0}
        if (not isinstance(state, dict) or set(state) != {"seen", "last_restart_ts"} or
                not isinstance(state["seen"], dict) or
                not finite_number(state["last_restart_ts"])):
            raise ValueError("invalid-kernel-ledger")
        for nonce, timestamp in state["seen"].items():
            if not NONCE.fullmatch(nonce) or not finite_number(timestamp):
                raise ValueError("invalid-kernel-ledger")
        self.state = state

    def save(self):
        cutoff = self.clock() - (self.config.max_age + 60)
        self.state["seen"] = {nonce: timestamp
                              for nonce, timestamp in self.state["seen"].items()
                              if timestamp >= cutoff}
        self.persist(self.config.kernel_state, self.state)

    def heartbeat_age(self):
        """返回 (age_seconds, detail)。内核从未部署过时返回 (None, 'no-heartbeat')。

        **不擅自重启**：心跳文件不存在说明内核从未在这台宿主上跑过，此时重启
        `guardian-kernel` 只会制造无意义的噪声。只有"曾经有心跳、现在过期了"
        才判定为卡死。
        """
        path = self.config.kernel_heartbeat
        if not path.exists():
            return None, "no-heartbeat"
        try:
            payload = strict_json(path.read_text(encoding="utf-8"))
            timestamp = float((payload or {}).get("ts"))
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
            return None, "unreadable-heartbeat"
        if not math.isfinite(timestamp):
            return None, "unreadable-heartbeat"
        return self.clock() - timestamp, "ok"

    def _restart(self, reason):
        now = self.clock()
        last = float(self.state.get("last_restart_ts") or 0.0)
        if now - last < self.config.kernel_cooldown:
            self.log("Kernel restart suppressed by cooldown: " + reason)
            return {"restarted": False, "reason": reason, "detail": "cooldown"}
        ok = self.docker.kernel_restart(self.config.kernel_container)
        self.state["last_restart_ts"] = now
        self.save()
        self.log("Kernel restart ({}) -> {}".format(reason, "ok" if ok else "failed"))
        return {"restarted": bool(ok), "reason": reason,
                "detail": "restarted" if ok else "restart-failed"}

    def consume_reload_request(self):
        processing = self.config.kernel_reload_processing
        if not processing.exists():
            if not self.config.kernel_reload_request.exists():
                return None
            try:
                os.replace(self.config.kernel_reload_request, processing)
            except OSError:
                return None
        try:
            payload = strict_json(processing.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            payload = None
        try:
            if not isinstance(payload, dict):
                result = {"restarted": False, "reason": "reload-request",
                          "detail": "invalid-request"}
            else:
                nonce, timestamp = payload.get("nonce"), payload.get("ts")
                if (not isinstance(nonce, str) or not NONCE.fullmatch(nonce) or
                        not finite_number(timestamp)):
                    result = {"restarted": False, "reason": "reload-request",
                              "detail": "invalid-request"}
                elif nonce in self.state["seen"]:
                    result = {"restarted": False, "reason": "reload-request",
                              "detail": "replay"}
                else:
                    age = self.clock() - timestamp
                    if age > self.config.max_age or age < -60:
                        result = {"restarted": False, "reason": "reload-request",
                                  "detail": "stale-request"}
                    else:
                        self.state["seen"][nonce] = self.clock()
                        self.save()
                        result = self._restart("reload-request")
        finally:
            try:
                processing.unlink()
            except FileNotFoundError:
                pass
        return result

    def tick(self):
        if not self.config.kernel_guard:
            return None
        try:
            with AdvisoryLock(self.config.kernel_lock) as acquired:
                if not acquired:
                    return None
                try:
                    self.load_state()
                except (OSError, ValueError, UnicodeError) as error:
                    self.log("Kernel ledger error: " + str(error))
                    return {"restarted": False, "reason": "ledger",
                            "detail": "ledger-error"}
                requested = self.consume_reload_request()
                if requested is not None:
                    return requested
                age, _detail = self.heartbeat_age()
                if age is None:
                    return None
                if age > self.config.kernel_timeout or age < -60:
                    return self._restart("heartbeat-timeout")
                return None
        except OSError as error:
            self.log("Kernel guard lock error: " + str(error))
            return None


class Watchdog:
    def __init__(self, config, docker=None, clock=time.time, persist=atomic_json):
        self.config = config
        self.clock = clock
        self.persist = persist
        self.docker = docker if docker is not None else Docker(config, log=self.log)
        self.ledger = None
        self.kernel_guard = KernelGuard(config, self.docker, clock, persist, self.log)
        self.last_kernel_result = None

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
        # 域 C 的内核心跳守护（B16 §4）：自带 flock，与请求处理互不阻塞。
        # 心跳守护失败不得拖垮请求处理，但必须留痕（I4：失败不静默）。
        try:
            self.last_kernel_result = self.kernel_guard.tick()
        except Exception as error:  # noqa: BLE001
            self.log("Kernel guard failed: " + str(error))
            self.last_kernel_result = {"restarted": False, "reason": "guard-error",
                                       "detail": str(error)}
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
