"""Durable host handoff. A submitted request is not completed sleep."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from ..core.models import ActionSpec
from .dockerclient import DockerError


def result_path(config) -> Path:
    explicit = config.safety.watchdog_result_path
    return Path(explicit) if explicit else Path(config.safety.watchdog_request_path).parent / "restart_result.json"


async def submit_sleep(engine, spec: ActionSpec, targets: list[str], duration: int, reason: str) -> str:
    path = Path(engine.config.safety.watchdog_request_path)
    if not engine.config.safety.watchdog_request_path:
        raise DockerError("watchdog_request_path 未配置")
    if engine.state.get("watchdog.pending"):
        raise DockerError("已有未完成的宿主机任务，不能覆盖")
    nonce = uuid.uuid4().hex
    payload = {"version": 2, "operation": "sleep", "nonce": nonce, "ts": time.time(),
               "targets": targets, "duration_min": duration, "reason": reason,
               "requested_by": "astrbot_plugin_guardian"}
    pending = {"nonce": nonce, "targets": targets, "ts": payload["ts"], "operation": "sleep"}
    engine.state.set("watchdog.pending", pending)
    # Commit before publishing: the host could stop this process immediately.
    # Snapshot the accounting keys first so a publication that never becomes visible
    # can be corrected into an honest failure instead of leaving a fake success
    # (which would silently reset the targets' fail streaks).
    accounting = engine.state.capture(("cooldowns", "action_history", "fail_streak"))
    engine.safety.commit(spec, True)
    spec.params["_watchdog_committed"] = True
    await engine.state.flush(force=True)

    published = False
    def publish():
        nonlocal published
        # StateStore's legacy writer logs IO errors. Verify durable handoff state.
        with engine.state.path.open("r", encoding="utf-8") as stream:
            if json.load(stream).get("watchdog.pending") != pending:
                raise OSError("pending state was not persisted")
        path.parent.mkdir(parents=True, exist_ok=True)
        if Path(str(path) + ".processing").exists():
            raise FileExistsError("host is consuming a previous request")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic no-clobber publication on Linux/NTFS. Never replace a queued request.
            os.link(temporary, path)
            published = True
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                os.unlink(temporary)
    try:
        await asyncio.to_thread(publish)
    except Exception as exc:
        if not published:
            # Nothing reached the host. Undo the optimistic success accounting and
            # record a real failure: an unpublished request must not reset the
            # targets' fail streaks, and the quota/cooldown it consumed stays
            # consumed (same policy as any other failed real action) so a broken
            # host handoff degrades to alert-only instead of looping.
            engine.state.set("watchdog.pending", None)
            engine.state.restore(accounting)
            engine.safety.commit(spec, False)
            await engine.state.flush(force=True)
        raise DockerError(f"宿主机请求提交{'后落盘确认' if published else ''}失败：{exc}；{'保留待确认任务' if published else '未发布请求'}") from exc
    return nonce


async def reconcile(engine) -> None:
    async with engine._watchdog_lock:
        await _reconcile_locked(engine)


async def _reconcile_locked(engine) -> None:
    pending = engine.state.get("watchdog.pending")
    if not isinstance(pending, dict) or not pending.get("nonce"):
        return
    try:
        def read():
            with result_path(engine.config).open("r", encoding="utf-8") as stream:
                return json.load(stream)
        result = await asyncio.to_thread(read)
    except (OSError, ValueError):
        return
    if (not isinstance(result, dict) or result.get("nonce") != pending["nonce"]
            or result.get("targets") != pending.get("targets")
            or result.get("operation") != "sleep"):
        return
    status = result.get("status")
    completed = status == "completed" and result.get("ok") is True
    failed = status in ("failed", "rejected") and result.get("ok") is False
    if not completed and not failed:
        return
    if completed:
        engine.lifecycle_policy.on_sleep_done(engine.lifecycle_state, time.time())
        engine.state.set("lifecycle.state", engine.lifecycle_state.to_dict())
    else:
        for target in pending["targets"]:
            engine.state.bump_fail_streak(target)
    engine.state.set("watchdog.pending", None)
    await engine.state.flush(force=True)
    engine.audit.append(kind="watchdog.result", target=",".join(pending["targets"]),
                        detail=str(result.get("detail", status)), ok=completed,
                        params={"nonce": pending["nonce"], "status": status})
    if engine.config.lifecycle.post_sleep_notify:
        from ..core.models import Severity
        text = ("[睡眠] 宿主机确认睡眠流程完成，全部目标已运行且健康检查通过。" if completed else
                f"[睡眠] 宿主机执行未成功：{result.get('detail', status)}。请检查宿主机执行记录。")
        try:
            await engine.notifier.push(text, severity=Severity.INFO if completed else Severity.CRIT, force=True)
        except Exception:
            pass
