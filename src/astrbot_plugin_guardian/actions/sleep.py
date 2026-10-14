"""Sleep action: delegate a self-sleep group to the durable host watchdog."""
from __future__ import annotations

import asyncio
import time

from ..core.log import logger

from ..core.models import ActionResult, ActionSpec, Severity
from ..utils.dockerclient import DockerError, DockerUnavailable
from ..utils.watchdog import submit_sleep
from .base import Action, register_action
from .container_actions import _verify_running


@register_action("sleep")
class SleepAction(Action):
    kind = "sleep"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.monotonic()
        targets = spec.params.get("targets", self.config.lifecycle.sleep_targets)
        duration = spec.params.get("duration_min", self.config.lifecycle.sleep_duration_min)
        reason = spec.params.get("reason") or spec.reason or "guardian sleep"
        decision = self.engine.safety.authorize(spec)
        def result(ok, detail, output=""):
            return ActionResult(ok, detail, int((time.monotonic() - started) * 1000), output)
        if not decision.allowed:
            return result(False, decision.reason)
        if spec.dry_run or decision.dry_run:
            return result(True, f"[dry-run] 将睡眠 {targets}，duration={duration}min，strategy={self.config.safety.self_restart_strategy}")
        try:
            await self.engine.notifier.push(
                f"[睡眠] 即将执行：目标 {', '.join(targets)}，时长 {duration} 分钟，原因：{reason}",
                severity=Severity.WARN, force=True)
        except Exception as exc:
            logger.warning(f"[guardian] 睡眠前通知失败: {exc}")
        if any(self.config.is_self(t) for t in targets):
            try:
                nonce = await submit_sleep(self.engine, spec, targets, duration, reason)
            except (DockerError, OSError) as exc:
                return result(False, str(exc))
            return result(True, f"宿主机睡眠请求已提交（nonce={nonce}），目标 {targets}；尚未确认停止或恢复。", "watchdog-submitted")

        attempted = []
        errors = []
        cancelled = False
        try:
            if duration > 0:
                for name in targets:
                    attempted.append(name)  # Even a failed stop can have stopped its target.
                    await self.docker.stop(name, timeout=10)
                await asyncio.sleep(duration * 60)
            else:
                for name in targets:
                    attempted.append(name)
                    await self.docker.restart(name, timeout=15)
        except asyncio.CancelledError:
            cancelled = True
            errors.append("睡眠任务取消，正在恢复已尝试的目标")
        except (DockerUnavailable, DockerError) as exc:
            errors.append(str(exc))
        finally:
            for name in attempted:
                try:
                    if duration > 0 or errors:
                        try:
                            await self.docker.start(name)
                        except DockerError as exc:
                            if exc.status != 304:
                                raise
                    ok, detail = await _verify_running(self.docker, name)
                    if not ok:
                        errors.append(f"{name}: {detail}")
                except (DockerUnavailable, DockerError) as exc:
                    errors.append(f"{name}: 恢复失败 {exc}")
        if cancelled:
            raise asyncio.CancelledError
        if errors:
            return result(False, "; ".join(errors))
        self.engine.lifecycle_policy.on_sleep_done(self.engine.lifecycle_state, time.time())
        self.engine.state.set("lifecycle.state", self.engine.lifecycle_state.to_dict())
        await self.engine.state.flush(force=True)
        if self.config.lifecycle.post_sleep_notify:
            try:
                await self.engine.notifier.push("[睡眠] 睡眠流程完成，全部目标已复核运行。", severity=Severity.INFO, force=True)
            except Exception:
                pass
        return result(True, f"睡眠完成并复核运行：{targets}")
