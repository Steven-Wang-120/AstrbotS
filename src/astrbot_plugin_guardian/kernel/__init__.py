"""Guardian 守护系统 —— 独立内核进程包（B16 §9 M3）。

入口：`python -m astrbot_plugin_guardian.kernel`

它把内核从 AstrBot 进程里**搬出来**，从而根治自杀悖论（B16 §1 S1）：
内核独立存活，重启 `astrbot` 不再是"自己杀自己"，而且它能在重启后**亲眼**
观察到 `astrbot` 的 `StartedAt` 更新与健康复检。

**本包不得 `import astrbot`**（I6）。
"""

from __future__ import annotations

from .service import KernelService, main  # noqa: F401

__all__ = ["KernelService", "main"]
