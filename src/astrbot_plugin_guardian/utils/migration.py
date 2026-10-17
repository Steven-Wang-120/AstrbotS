"""Guardian 守护系统 —— 数据目录迁移（B16 §7 / §9 M4）。

内核独立后，业务状态（`state.json` / `audit.jsonl`）要从 AstrBot 的插件数据目录
搬到**内核可见的共享卷**——否则内核重启（或 AstrBot 容器重建）会看到一份空的
状态，冷却、配额、内存样本环全部丢失（B16 §7 的"内存样本环需跨 AstrBot 重启
保持"正是内核独立后的新能力）。

迁移必须满足两条硬约束（B16 §9 M4 的验收与回滚）：

1. **幂等**：重复启动不得产生副作用。靠 `.migrated` 标记 + "目标已有 state.json
   就不动"双保险。
2. **可回滚**：只**复制**、绝不删除。旧目录原样保留为只读副本，回滚 = 把新目录
   挪开、继续用旧目录。

本模块纯 stdlib，不得 `import astrbot`（I6）。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

#: 迁移完成标记（写在**新**状态目录里）
MIGRATION_MARKER = ".migrated"

#: 需要迁移的业务文件（单写者 = 内核，I1）
MIGRATED_FILES = ("state.json", "audit.jsonl")

#: AstrBot 侧历史插件数据目录的相对路径（相对共享卷）
LEGACY_RELATIVE = ("plugin_data", "astrbot_plugin_guardian")

#: 业务文件实际所在的子目录。`StateStore` / `AuditLog` 都写 `<数据目录>/guardian/`，
#: 迁移必须落在**同一处**，否则内核读到的还是一片空白。
STATE_SUBDIR = "guardian"


def resolve_legacy_data_dir(config, data_dir: str) -> str:
    """解析"旧数据目录"。

    优先级：显式配置 `kernel.legacy_data_dir` → 由 `kernel.shared_dir` 推断的
    AstrBot 规范目录 → 空（不迁移）。**不猜测**：拿不准就不动，宁可不迁移也不能
    把状态搬错地方。
    """
    kernel = getattr(config, "kernel", None)
    explicit = str(getattr(kernel, "legacy_data_dir", "") or "").strip()
    if explicit:
        return explicit
    shared = str(getattr(kernel, "shared_dir", "") or "").strip()
    if shared:
        return os.path.join(shared, *LEGACY_RELATIVE)
    return ""


def locate_state_dir(base) -> Path | None:
    """在"数据目录"里定位真正存放 `state.json` 的那一层。

    规范形态是 `<数据目录>/guardian/`；但历史部署里也出现过直接放在数据目录根下的
    形态。优先嵌套目录，其不存在时回退根目录；两者都没有则返回 None（不动）。
    """
    base = Path(base)
    nested = base / STATE_SUBDIR
    if (nested / "state.json").exists():
        return nested
    if (base / "state.json").exists():
        return base
    return None


def migrate_legacy_data(new_dir: str, legacy_dir: str, *,
                        marker: str = MIGRATION_MARKER) -> dict:
    """把旧目录的业务文件复制到新目录；幂等、非覆盖、可回滚。

    `new_dir` / `legacy_dir` 都是**数据目录**（业务文件在其 `guardian/` 子目录下）。
    返回 `{"migrated": bool, "copied": [...], "skipped": [...], "reason": str}`。
    任何一步拿不准都返回 `migrated=False` 并给出 `reason`，**绝不半途改坏数据**。
    """
    result: dict = {"migrated": False, "copied": [], "skipped": [], "reason": ""}
    if not legacy_dir:
        result["reason"] = "no-legacy-dir"
        return result
    new = Path(new_dir)
    legacy = Path(legacy_dir)
    try:
        if legacy.resolve() == new.resolve():
            result["reason"] = "same-dir"
            return result
    except OSError:  # pragma: no cover - 极端路径
        result["reason"] = "path-unresolvable"
        return result

    legacy_state_dir = locate_state_dir(legacy)
    if legacy_state_dir is None:
        result["reason"] = "no-legacy-state"
        return result
    target = new / STATE_SUBDIR
    marker_path = target / marker
    if marker_path.exists():
        result["reason"] = "already-migrated"
        return result
    if (target / "state.json").exists():
        # 目标已有内核状态：这是"已经在新位置跑过"，不是"需要迁移"。
        result["reason"] = "target-has-state"
        return result

    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in MIGRATED_FILES:
            source = legacy_state_dir / name
            if not source.is_file():
                continue
            destination = target / name
            if destination.exists():
                result["skipped"].append(name)
                continue
            shutil.copy2(source, destination)
            result["copied"].append(name)
        if not result["copied"]:
            result["reason"] = "nothing-copied"
            return result
        payload = {"migrated_at": time.time(), "source": str(legacy_state_dir),
                   "files": list(result["copied"]), "version": 1}
        temporary = Path(str(marker_path) + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(temporary, marker_path)
    except OSError as error:
        result["reason"] = f"io-error:{error}"
        return result
    result["migrated"] = True
    result["reason"] = "migrated"
    return result


__all__ = ["MIGRATION_MARKER", "MIGRATED_FILES", "LEGACY_RELATIVE",
           "STATE_SUBDIR", "resolve_legacy_data_dir", "locate_state_dir",
           "migrate_legacy_data"]
