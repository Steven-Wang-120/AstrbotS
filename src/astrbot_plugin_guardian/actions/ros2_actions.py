"""Guardian 守护系统 —— ROS2 恢复动作。

对应文档：B07 §7

安全：恢复命令只能来自配置 `ros2.node_restart_commands`（白名单映射），
且执行前经 `validate_command()` 二次校验。
"""

from __future__ import annotations

import asyncio
import time

from ..core.models import ActionResult, ActionSpec
from ..utils.ros2_runner import parse_node_list
from .base import Action, register_action


@register_action("ros2_restart")
class Ros2RestartNodeAction(Action):
    kind = "ros2_restart"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.time()
        node = spec.params.get("node") or spec.target
        if spec.dry_run:
            return ActionResult(
                True, f"[dry-run] 将执行 {node} 的恢复命令（白名单）",
                int((time.time() - started) * 1000))
        out = await self.ros2.restart_node(node)
        elapsed = int((time.time() - started) * 1000)
        if not out.ok:
            return ActionResult(False, f"恢复 {node} 失败(rc={out.rc}): {out.text[:200]}",
                                elapsed)
        ok, detail = await self._verify_node(node)
        return ActionResult(ok, f"已执行 {node} 恢复命令，复核: {detail}", elapsed)

    async def _verify_node(self, node: str, attempts: int = 3,
                           wait: float = 4.0) -> tuple[bool, str]:
        last = "未知"
        for i in range(attempts):
            await asyncio.sleep(wait)
            out = await self.ros2.node_list()
            if not out.ok:
                last = f"node list 失败 rc={out.rc}"
                continue
            found = parse_node_list(out.text)
            if node in found:
                return True, "节点已恢复"
            last = f"节点仍缺失（当前 {len(found)} 个）"
        return False, last
