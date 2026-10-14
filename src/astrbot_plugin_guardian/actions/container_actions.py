"""Guardian 守护系统 —— 容器动作。

对应文档：B04 §6

- RestartContainerAction / StartContainerAction
- 重启后复核（等待并重新 inspect）
- 受保护容器与自重启策略由 SafetyGuard + SleepAction 处理
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

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
