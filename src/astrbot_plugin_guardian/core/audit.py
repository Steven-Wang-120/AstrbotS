"""Guardian 守护系统 —— 审计日志（追加式 JSONL）。

对应文档：B11 §审计

- 文件：<data>/guardian/audit.jsonl
- 写入失败只告警，绝不抛异常（审计不可阻塞守护主流程）
- token / access_token 等敏感字段递归脱敏
- 超过容量上限自动轮转
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

SENSITIVE_KEYS = {"access_token", "token", "webui_token", "authorization", "password", "secret"}
MAX_BYTES = 8 * 1024 * 1024  # 8MB 轮转
KEEP_ROTATIONS = 3


def redact(obj: Any) -> Any:
    """递归脱敏。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in SENSITIVE_KEYS:
                out[k] = "***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 500:
        return obj[:500] + "...(truncated)"
    return obj


class AuditLog:
    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir) / "guardian"
        self.path = self.dir / "audit.jsonl"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            logger.warning(f"[guardian] 无法创建审计目录 {self.dir}: {exc}")

    # ------------------------------------------------------------------ #
    def append(self, *, kind: str = "", target: str = "", detail: str = "",
               actor: str = "system", trigger: str = "interval", ok: bool | None = None,
               elapsed_ms: int = 0, suppressed: bool = False, reason: str = "",
               params: dict[str, Any] | None = None,
               incident: Any | None = None) -> None:
        """写一条审计。

        兼容两种调用风格：
        - append(kind=..., target=..., detail=...)
        - append(incident=<Incident>)  # kind 固定为 "incident"

        `kind` 必须具备默认值：engine._record_incident() 走 incident= 分支，
        若 kind 必填则该路径 TypeError，任何产生 finding 的巡检都会整体崩溃。
        """
        if incident is not None:
            kind = kind or "incident"
            record = {
                "ts": getattr(incident, "ts", time.time()),
                "kind": "incident",
                "actor": getattr(getattr(incident, "action", None), "actor", "system"),
                "trigger": getattr(incident, "trigger", "interval"),
                "probe": getattr(incident, "probe", ""),
                "target": getattr(incident, "target", ""),
                "status": str(getattr(getattr(incident, "status", ""), "value", "")),
                "severity": str(getattr(getattr(incident, "severity", ""), "value", "")),
                "detail": getattr(incident, "message", ""),
                "action": getattr(getattr(incident, "action", None), "kind", ""),
                "params": getattr(getattr(incident, "action", None), "params", None),
                "ok": getattr(getattr(incident, "result", None), "ok", None),
                "elapsed_ms": getattr(getattr(incident, "result", None), "elapsed_ms", 0),
                "suppressed": getattr(incident, "suppressed", False),
                "reason": getattr(incident, "suppress_reason", ""),
            }
        else:
            record = {
                "ts": time.time(),
                "kind": kind,
                "actor": actor,
                "trigger": trigger,
                "probe": "",
                "target": target,
                "status": "",
                "severity": "",
                "detail": detail,
                "action": kind,
                "params": params,
                "ok": ok,
                "elapsed_ms": elapsed_ms,
                "suppressed": suppressed,
                "reason": reason,
            }
        record = redact(record)
        line = json.dumps(record, ensure_ascii=False)
        try:
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:  # pragma: no cover
            logger.warning(f"[guardian] 审计写入失败: {exc}")

    # ------------------------------------------------------------------ #
    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size >= MAX_BYTES:
                for i in range(KEEP_ROTATIONS - 1, 0, -1):
                    src = self.path.with_suffix(f".jsonl.{i}")
                    dst = self.path.with_suffix(f".jsonl.{i + 1}")
                    if src.exists():
                        os.replace(src, dst)
                os.replace(self.path, self.path.with_suffix(".jsonl.1"))
        except OSError as exc:  # pragma: no cover
            logger.warning(f"[guardian] 审计轮转失败: {exc}")

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        """读取最近 limit 条审计（用于 /guardian incidents 兜底）。"""
        if not self.path.exists():
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
            out = []
            for ln in lines:
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
            return out
        except OSError:
            return []
