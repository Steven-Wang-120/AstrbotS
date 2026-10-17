"""Guardian 守护系统 —— 档位 / 模式切换动作。

对应文档：B00 §4-§5、B02 §6
"""

from __future__ import annotations

import time

from ..core.models import ActionResult, ActionSpec, Mode, Profile
from .base import Action, register_action


@register_action("set_profile")
class SetProfileAction(Action):
    kind = "set_profile"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.time()
        profile = str(spec.params.get("profile") or "normal")
        if profile not in {p.value for p in Profile}:
            return ActionResult(False, f"非法档位 {profile}")
        if spec.dry_run:
            return ActionResult(True, f"[dry-run] 将切换档位 -> {profile}")
        await self.engine.apply_profile(profile, reason=spec.reason or "action")
        return ActionResult(True, f"档位已切换为 {profile}",
                            int((time.time() - started) * 1000))


@register_action("set_mode")
class SetModeAction(Action):
    kind = "set_mode"

    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.time()
        mode = str(spec.params.get("mode") or "server")
        if mode not in {m.value for m in Mode}:
            return ActionResult(False, f"非法模式 {mode}")
        if spec.dry_run:
            return ActionResult(True, f"[dry-run] 将切换模式 -> {mode}")
        await self.engine.apply_mode(mode, reason=spec.reason or "action")
        return ActionResult(True, f"模式已切换为 {mode}",
                            int((time.time() - started) * 1000))
