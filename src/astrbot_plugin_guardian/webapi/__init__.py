"""Guardian 守护系统 —— 域 B 的 WebUI 扩展（B17 §10）。

本包允许 `import astrbot`（dashboard 集成），但**不得**触碰 docker / 动作 /
安全判定——所有动作都经 `runtime.request_qq_login()` 转发内核。
"""

from __future__ import annotations

__all__ = ["login_page"]
