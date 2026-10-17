"""Guardian 守护系统 —— 容器动作。

对应文档：B04 §6

- RestartContainerAction / StartContainerAction
- 重启后复核（等待并重新 inspect）
- 受保护容器与自重启策略由 SafetyGuard + SleepAction 处理
"""

from __future__ import annotations

import asyncio
import time

from ..core.log import logger

from ..core.models import ActionResult, ActionSpec
from ..utils.dockerclient import DockerError, DockerUnavailable
from .base import Action, register_action


async def _verify_running(docker, name: str, *, wait: float = 6.0,
                          interval: float = 1.5) -> tuple[bool, str]:
    """重启/启动后复核容器是否 running。"""
    deadline = time.time() + wait
    last = ""
    while time.time() < deadline:
        try:
            data = await docker.inspect(name)
            state = data.get("State") or {}
            if state.get("Running"):
                return True, "running"
            last = str(state.get("Status") or "unknown")
        except DockerError as exc:
            last = f"inspect error {exc}"
        except DockerUnavailable as exc:
            return False, f"daemon 不可达: {exc}"
        await asyncio.sleep(interval)
    return False, f"复核未通过（最后状态 {last or 'unknown'}）"


async def _verify_restarted(docker, name: str, before_started_at: str = "", *,
                            wait: float = 30.0,
                            interval: float = 1.5) -> tuple[bool, str]:
    """D2-b 的"亲眼看到"：running + 健康 + `StartedAt` 已更新。

    M5 的核心收益就在这里——内核搬出 AstrBot 进程后，发起者不再随 `astrbot`
    一起死，所以它能**独立确认**一次重启/睡眠是否真的发生，而不是只把请求丢出去
    就以为成了（B16 §4 的关键结论）。

    判据（2026-10-16 实机 C16-K3 校准）：

    - `Running` 必须为真；
    - 有 healthcheck 时 **`unhealthy` 判失败**（C16-29），但 `starting` 只是
      **过渡态**——重启已经确认发生、应用还在起，把它当失败会产生假阴性：
      实测 `astrbot-local` 需要 ~14s 才 healthy，而旧实现只等 8s，于是每次
      真机睡眠都被报成"复核未通过"；
    - `StartedAt` 必须变化（拿不到 `before_started_at` 时跳过比对）。

    `wait` 默认 30s：只覆盖"启动 + 变健康"的正常耗时；一旦满足判据立即返回，
    所以真正的失败路径不会被拖满窗口。
    """
    deadline = time.time() + wait
    last = ""
    while time.time() < deadline:
        try:
            data = await docker.inspect(name)
        except DockerUnavailable as exc:
            return False, f"daemon 不可达: {exc}"
        except DockerError as exc:
            last = f"inspect error {exc}"
            await asyncio.sleep(interval)
            continue
        state = data.get("State") or {}
        if state.get("Running") is not True:
            last = str(state.get("Status") or "unknown")
            await asyncio.sleep(interval)
            continue
        started = str(state.get("StartedAt") or "")
        health = str((state.get("Health") or {}).get("Status") or "")
        if health == "unhealthy":
            # 明确不健康：不算成功，但继续等到窗口结束（有自愈的可能）
            last = "health=unhealthy"
            await asyncio.sleep(interval)
            continue
        if before_started_at and started and started == before_started_at:
            last = "StartedAt 未更新（容器未真正重启）"
            await asyncio.sleep(interval)
            continue
        return True, f"running, started={started or 'n/a'}, health={health or 'n/a'}"
    return False, f"复核未通过（最后状态 {last or 'unknown'}）"


@register_action("restart_container")
class RestartContainerAction(Action):
    kind = "restart_container"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.time()
        name = spec.target
        timeout = int(spec.params.get("timeout", 10))
        if self.config.is_self(name):
            from dataclasses import replace
            from .sleep import SleepAction
            params = dict(spec.params, targets=[name], duration_min=0)
            delegated = replace(spec, kind="sleep", params=params)
            result = await SleepAction(self.config, self.deps).execute(delegated)
            if delegated.params.get("_watchdog_committed"):
                spec.params["_watchdog_committed"] = True
            return result
        if spec.dry_run:
            return ActionResult(True, f"[dry-run] 将重启容器 {name}",
                                int((time.time() - started) * 1000))
        try:
            await self.docker.restart(name, timeout=timeout)
        except (DockerUnavailable, DockerError) as exc:
            return ActionResult(False, f"重启 {name} 失败: {exc}",
                                int((time.time() - started) * 1000))
        ok, detail = await _verify_running(self.docker, name)
        return ActionResult(ok, f"已重启 {name}，复核: {detail}",
                            int((time.time() - started) * 1000))


@register_action("start_container")
class StartContainerAction(Action):
    kind = "start_container"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.time()
        name = spec.target
        if spec.dry_run:
            return ActionResult(True, f"[dry-run] 将启动容器 {name}",
                                int((time.time() - started) * 1000))
        try:
            await self.docker.start(name)
        except DockerError as exc:
            if exc.status == 304:  # 已在运行
                return ActionResult(True, f"容器 {name} 已在运行",
                                    int((time.time() - started) * 1000))
            return ActionResult(False, f"启动 {name} 失败: {exc}",
                                int((time.time() - started) * 1000))
        except DockerUnavailable as exc:
            return ActionResult(False, f"Docker 不可达: {exc}",
                                int((time.time() - started) * 1000))
        ok, detail = await _verify_running(self.docker, name)
        return ActionResult(ok, f"已启动 {name}，复核: {detail}",
                            int((time.time() - started) * 1000))


@register_action("notify")
class NotifyAction(Action):
    kind = "notify"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        from ..core.models import Severity
        text = spec.params.get("text") or spec.reason or "守护系统通知"
        try:
            await self.engine.notifier.push(text, severity=Severity.INFO, force=True)
            return ActionResult(True, "已发送通知")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 通知失败: {exc}")
            return ActionResult(False, f"通知失败: {exc}")
