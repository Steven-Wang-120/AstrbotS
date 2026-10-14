"""Guardian 守护系统 —— QQ 重登动作。

对应文档：B05 §5

三种策略：
- restart_container（默认）：重启 napcat 容器，借助已保存会话快速登录
- webui_quick_login：调用 NapCat WebUI 触发快速登录
- disabled：只告警，不动作
"""

from __future__ import annotations

import asyncio
import time

from ..core.log import logger

from ..core.models import ActionResult, ActionSpec
from ..utils.dockerclient import DockerError, DockerUnavailable
from ..utils.napcat import NapCatError
from .base import Action, register_action


@register_action("qq_relogin")
class QqReloginAction(Action):
    kind = "qq_relogin"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.time()
        method = self.config.qq.relogin_method or "restart_container"
        napcat_name = self.config.containers.napcat

        if spec.dry_run:
            return ActionResult(True, f"[dry-run] 将按 {method} 重登 QQ",
                                int((time.time() - started) * 1000))

        if method == "disabled":
            return ActionResult(
                False, "重登策略为 disabled，仅告警（请人工处理）",
                int((time.time() - started) * 1000))

        if method == "restart_container":
            try:
                await self.docker.restart(napcat_name, timeout=15)
            except (DockerUnavailable, DockerError) as exc:
                return ActionResult(False, f"重启 {napcat_name} 失败: {exc}",
                                    int((time.time() - started) * 1000))
        elif method == "webui_quick_login":
            try:
                await self.napcat.webui_quick_login()
            except NapCatError as exc:
                return ActionResult(
                    False, f"WebUI 快速登录失败（可能需人工扫码）: {exc}",
                    int((time.time() - started) * 1000))
        else:
            return ActionResult(False, f"未知重登策略 {method}",
                                int((time.time() - started) * 1000))

        ok, detail = await self._verify_login()
        elapsed = int((time.time() - started) * 1000)
        if ok:
            self.state.set("qq_fail_streak", 0)
            return ActionResult(True, f"重登成功：{detail}", elapsed)
        return ActionResult(False, f"重登后仍未在线：{detail}（可能需人工扫码）", elapsed)

    async def _verify_login(self, attempts: int = 4, wait: float = 6.0) -> tuple[bool, str]:
        last = "无响应"
        for i in range(attempts):
            await asyncio.sleep(wait if i else max(2.0, wait / 2))
            try:
                info = await self.napcat.get_login_info()
                data = info.get("data") if isinstance(info.get("data"), dict) else info
                uid = str(data.get("user_id") or "").strip()
                if uid and uid not in ("0", "None"):
                    return True, f"user_id={uid} nickname={data.get('nickname') or '-'}"
                last = "user_id 为空"
            except NapCatError as exc:
                last = str(exc)
                logger.debug(f"[guardian] 重登复核第 {i + 1} 次失败: {exc}")
        return False, last
