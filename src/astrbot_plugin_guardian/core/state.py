"""Guardian 守护系统 —— 状态持久化。

对应文档：B11

- 落盘目录：<AstrBot data>/guardian/（不是插件目录）
- 原子写：临时文件 + os.replace
- 损坏兜底：备份为 .corrupt.<ts> 后重建
- schema 版本迁移
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .log import logger

SCHEMA_VERSION = 2

# 需要跨重启保留的键
PERSISTED_KEYS = {
    "mode",
    "profile",
    "night_until",
    "lifecycle.state",
    "lifecycle.inject_prompt",
    "lifecycle.inject_level",
    "cooldowns",          # target -> ts
    "action_history",     # [ts, ...]
    "fail_streak",        # target -> int
    "tripped",            # bool
    "tripped_at",
    "last_status",        # key -> status value（跃迁告警基线）
    "qq_fail_streak",
    # B17 §13：登录恢复的状态必须跨重启保留。否则插件重载一次就丢掉静默期，
    # 密码试错保护形同虚设；WebUI 登录状态页也会看不到"最近一次失败原因"。
    "qq.auth_hold_until",
    "qq.last_failure",
    "qq.last_success_at",
    "samples",            # container -> [[ts, mb], ...]
    "memory.sample_meta", # 实例/口径标识与连续确认，随样本一起持久化
    "watchdog.pending",   # 宿主机任务 nonce/目标，提交不等于睡眠完成
    "umos_invalid",       # [umo, ...]
}


class StateStore:
    """内存态 + 原子 JSON 落盘。"""

    def __init__(self, data_dir: str | Path, *, flush_interval: float = 20.0) -> None:
        self.dir = Path(data_dir) / "guardian"
        self.path = self.dir / "state.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._flush_interval = flush_interval
        self._last_flush = 0.0
        self._ensure_dir()

    # ------------------------------------------------------------------ #
    def _ensure_dir(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover
            logger.warning(f"[guardian] 无法创建状态目录 {self.dir}: {exc}")

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if not self.path.exists():
            self._data = {"schema": SCHEMA_VERSION}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise ValueError("state.json 根节点不是对象")
            self._data = self._migrate(payload)
        except Exception as exc:  # noqa: BLE001
            backup = self.path.with_suffix(f".corrupt.{int(time.time())}.json")
            try:
                shutil.copy2(self.path, backup)
            except OSError:
                pass
            logger.warning(
                f"[guardian] state.json 损坏({exc})，已备份到 {backup.name} 并重建")
            self._data = {"schema": SCHEMA_VERSION}

    def _migrate(self, payload: dict[str, Any]) -> dict[str, Any]:
        ver = int(payload.get("schema", 1) or 1)
        if ver < 2:
            # v1 -> v2：lifecycle 状态独立成对象
            payload.setdefault("lifecycle.state", {})
            payload["schema"] = 2
        return payload

    # ------------------------------------------------------------------ #
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._dirty = True

    def update(self, mapping: dict[str, Any]) -> None:
        self._data.update(mapping)
        self._dirty = True

    def snapshot(self) -> dict[str, Any]:
        """返回仅含持久化键的浅拷贝，供展示与落盘。"""
        return {k: v for k, v in self._data.items() if k in PERSISTED_KEYS or k == "schema"}

    # ---- 回滚支持 ----------------------------------------------------- #
    def capture(self, keys: tuple[str, ...]) -> dict[str, Any]:
        """深拷贝指定键的当前值，供"乐观记账后失败"的场景回滚。

        典型用法：宿主机交接必须在发布前先记账（否则进程被宿主机杀掉就丢了动作记录），
        但发布可能失败。失败时用本快照还原，避免把一次没发生的动作记成成功。
        """
        return {k: copy.deepcopy(self._data.get(k)) for k in keys}

    def restore(self, snapshot: dict[str, Any]) -> None:
        """把 capture() 的快照写回；快照值为 None 的键会被删除。"""
        for key, value in snapshot.items():
            if value is None:
                self._data.pop(key, None)
            else:
                self._data[key] = copy.deepcopy(value)
        self._dirty = True

    # ------------------------------------------------------------------ #
    async def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and not self._dirty:
            return
        if not force and (now - self._last_flush) < self._flush_interval:
            return
        async with self._lock:
            # 快照必须在**事件循环线程**里取。
            #
            # `_write_atomic` 经 `asyncio.to_thread` 在工作线程里执行；若让它自己
            # 去遍历 `self._data`，而事件循环同时 `set()` 了新键（探针回调、
            # 引擎记账都会做这件事），就会抛
            # `RuntimeError: dictionary changed size during iteration`，
            # 使整轮巡检以一条费解的报错失败、并丢掉这次状态落盘。
            #
            # 在事件循环线程里先构造好 payload（其间没有 await，不会被其他协程
            # 插入），再把不可变的快照交给工作线程，即可根除该竞争。
            payload = self.snapshot()
            await asyncio.to_thread(self._write_atomic, payload)
            self._dirty = False
            self._last_flush = now

    def _write_atomic(self, payload: dict[str, Any] | None = None) -> None:
        self._ensure_dir()
        if payload is None:  # 兼容直接调用（测试/诊断）
            payload = self.snapshot()
        tmp = str(self.path) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover
            logger.warning(f"[guardian] 状态落盘失败: {exc}")

    # ---- 便捷方法 ----------------------------------------------------- #
    def bump_cooldown(self, target: str, ts: float | None = None) -> None:
        cds = self._data.setdefault("cooldowns", {})
        cds[target] = ts if ts is not None else time.time()
        self._dirty = True

    def cooldown_of(self, target: str) -> float:
        return float((self._data.get("cooldowns") or {}).get(target, 0.0) or 0.0)

    def record_action(self, ts: float | None = None) -> None:
        hist = self._data.setdefault("action_history", [])
        hist.append(ts if ts is not None else time.time())
        # 只保留 24h
        cutoff = time.time() - 86400
        self._data["action_history"] = [t for t in hist if t >= cutoff]
        self._dirty = True

    def actions_in_window(self, window_sec: int) -> int:
        cutoff = time.time() - window_sec
        return len([t for t in (self._data.get("action_history") or []) if t >= cutoff])

    def bump_fail_streak(self, target: str) -> int:
        fs = self._data.setdefault("fail_streak", {})
        fs[target] = int(fs.get(target, 0)) + 1
        self._dirty = True
        return fs[target]

    def reset_fail_streak(self, target: str) -> None:
        fs = self._data.setdefault("fail_streak", {})
        fs[target] = 0
        self._dirty = True
