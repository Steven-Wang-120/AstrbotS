"""Sleep action: 内核直连（D2-b）或经宿主看门狗（回滚路径）。

对应文档：B15（生命周期与自主睡眠）、B16 §4/§9 M5（动作直连化）

两条路径的分工：

- **直连（D2-b，M5 默认）**：`kernel.mode=ipc` 且 `kernel.direct_actions=true` 时，
  内核自己 `stop → 等待 → start → 复核 StartedAt + 健康`，然后调 `on_sleep_done()`
  清 `forced_at`。**这正是"把内核搬出去"的收益兑现处**：发起者不再随 `astrbot`
  一起死，所以它能亲眼看到睡眠真的完成（B16 §1 S1 被根治）。
- **经看门狗（回滚 / inproc）**：同进程内核重启 `astrbot` 会自杀，或运维显式
  `kernel.direct_actions=false` 时，退回"写请求文件 → 宿主看门狗兑现"的旧路径。

直连路径下若 docker 访问失效（如 socket 权限变更），内核**退回**写请求文件，
由看门狗兜底（B16 §4 / §13 K9），而不是假装成功。
"""

from __future__ import annotations

import asyncio
import time

from ..core.log import logger

from ..core.models import ActionResult, ActionSpec, Severity
from ..utils.dockerclient import DockerError, DockerUnavailable
from ..utils.watchdog import submit_sleep
from .base import Action, direct_actions_enabled, register_action
from .container_actions import _verify_restarted


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

        self_targets = [t for t in targets if self.config.is_self(t)]
        if self_targets and not direct_actions_enabled(self.config):
            # 回滚路径 / inproc：同进程内核重启 astrbot 会自杀（S1），交给宿主看门狗。
            return await self._execute_via_watchdog(spec, targets, duration, reason, result)
        return await self._execute_direct(spec, targets, duration, reason, result)

    # ------------------------------------------------------------------ #
    async def _execute_via_watchdog(self, spec, targets, duration, reason, result):
        try:
            nonce = await submit_sleep(self.engine, spec, targets, duration, reason)
        except (DockerError, OSError) as exc:
            return result(False, str(exc))
        # 请求已落盘，睡眠会由看门狗真正执行 → 登记睡眠窗口，让本轮起的巡检
        # 不再把"正在被睡下的容器"当成故障去重启（否则巡检会打断睡眠）。
        # 窗口由 `wake_at` 自动失效，也可由 `reconcile` 在收到结果时清除。
        self.engine.mark_sleeping(targets, duration)
        return result(
            True,
            f"宿主机睡眠请求已提交（nonce={nonce}），目标 {targets}；尚未确认停止或恢复。",
            "watchdog-submitted")

    async def _execute_direct(self, spec, targets, duration, reason, result):
        """D2-b 直连：内核自己动容器，并自行复检 `StartedAt` + 健康。"""
        before = await self._capture_started_at(targets)
        # 先登记睡眠窗口再动手：`stop` 一落地，下一趟巡检就会看到 `exited`。
        # 没有这道闸门，巡检会 `start_container` 打断睡眠，而且会把睡眠末尾的
        # `StartedAt` 复核"喂"成假阳性通过（2026-10-16 实机 C16-K3）。
        self.engine.mark_sleeping(targets, duration)

        attempted: list[str] = []
        errors: list[str] = []
        cancelled = False
        unreachable = False
        daemon_reached = False
        try:
            if duration > 0:
                for name in targets:
                    attempted.append(name)  # 即便 stop 失败，也可能已经停掉了目标
                    await self.docker.stop(name, timeout=10)
                    daemon_reached = True
                await asyncio.sleep(duration * 60)
            else:
                for name in targets:
                    attempted.append(name)
                    await self.docker.restart(name, timeout=15)
                    daemon_reached = True
        except asyncio.CancelledError:
            cancelled = True
            errors.append("睡眠任务取消，正在恢复已尝试的目标")
        except DockerUnavailable as exc:
            # daemon/socket 不可达 —— 这正是"内核 docker 访问失效"（B16 §4 / K9）。
            unreachable = True
            errors.append(f"Docker 不可达: {exc}")
        except DockerError as exc:
            daemon_reached = True  # daemon 有应答，只是这次操作失败
            errors.append(str(exc))
        finally:
            if not unreachable:
                for name in attempted:
                    try:
                        if duration > 0 or errors:
                            try:
                                await self.docker.start(name)
                            except DockerError as exc:
                                if exc.status != 304:
                                    raise
                        ok, detail = await _verify_restarted(
                            self.docker, name, before.get(name, ""))
                        if not ok:
                            errors.append(f"{name}: {detail}")
                    except (DockerUnavailable, DockerError) as exc:
                        errors.append(f"{name}: 恢复失败 {exc}")
        if cancelled:
            raise asyncio.CancelledError

        if unreachable and not daemon_reached and any(self.config.is_self(t) for t in targets):
            # 一个目标都没碰过（首个 docker 调用就不可达）→ 退回写请求文件，
            # 由宿主看门狗兜底（B16 §4）。绝不假装成功。
            self.audit.append(kind="action.fallback", target=",".join(targets),
                              detail="内核 docker 访问失效，退回宿主看门狗兜底",
                              ok=False, actor=spec.actor, trigger=spec.trigger)
            try:
                nonce = await submit_sleep(self.engine, spec, targets, duration, reason)
            except (DockerError, OSError) as exc:
                self.engine.clear_sleeping(targets)   # 没有任何睡眠会发生
                return result(False, f"直连不可用且回退看门狗失败：{exc}")
            # 睡眠交给看门狗执行 → **保留**窗口，等 `reconcile` 收到结果或窗口过期。
            return result(True,
                          f"内核 docker 不可达，已退回宿主看门狗（nonce={nonce}）",
                          "watchdog-submitted")

        if errors:
            self.engine.clear_sleeping(targets)
            return result(False, "; ".join(errors))
        self.engine.lifecycle_policy.on_sleep_done(self.engine.lifecycle_state, time.time())
        self.engine.clear_sleeping(targets)
        self.engine.state.set("lifecycle.state", self.engine.lifecycle_state.to_dict())
        await self.engine.state.flush(force=True)
        if self.config.lifecycle.post_sleep_notify:
            try:
                await self.engine.notifier.push("[睡眠] 睡眠流程完成，全部目标已复核运行。", severity=Severity.INFO, force=True)
            except Exception:
                pass
        return result(True, f"直连睡眠完成并复核运行（StartedAt 已更新）：{targets}", "direct")

    # ------------------------------------------------------------------ #
    async def _capture_started_at(self, targets) -> dict[str, str]:
        before: dict[str, str] = {}
        for name in targets:
            try:
                data = await self.docker.inspect(name)
                before[name] = str((data.get("State") or {}).get("StartedAt") or "")
            except (DockerUnavailable, DockerError):
                before[name] = ""
        return before
