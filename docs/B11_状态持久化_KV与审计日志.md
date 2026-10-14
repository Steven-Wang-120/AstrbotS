# B11 · 状态持久化、KV 与审计日志

> 上位规格：`B00_项目总览与需求规格.md`（唯一权威）。实现 B00 §3.2（数据流记录点）、§7（`llm_tools`/`schedule`/`night_profile` 相关状态）、§10（审计不可关、参数校验）、§13（Gate `G-SAFETY`/`G-STABILITY`）；ADR D5（状态落 `data/`）、R6（状态文件损坏）。关联 B02（配置与运行时边界）、B03（引擎记录点）、B08（通知审计）、B09（工具审计）、B10（调度器）、C11（测试）。版本 v0.1.0，日期 2026-10-13。

## 1. 目标

| ID | 目标 | 判据 |
| --- | --- | --- |
| S-G1 | 跨重启保留关键状态（档位/模式/夜间截止/冷却/熔断/事件历史/采样窗口/跃迁基线） | 重启后值不变（C11-1/2） |
| S-G2 | 落盘原子、可恢复，坏文件不导致启动失败 | 注入半截/非法 JSON 后仍能启动（C11-5/6） |
| S-G3 | schema 版本化并可迁移 | 旧版本文件加载后升级且数据保留（C11-7） |
| S-G4 | 审计追加式、不可关、可轮转、脱敏 | 每次动作含被抑制者均有记录（C11-10） |
| S-G5 | 事件历史环形缓冲，供 CLI 与 LLM 查询 | `recent_incidents(N)` 返回最近 N 条（C11-12） |
| S-G6 | KV 与文件职责清晰（文件权威、KV 仅镜像少量标量）；数据落 `data/`，更新/重装不丢 | 文件权威（C11-13）；目录位于 `StarTools.get_data_dir()`（C11-16） |

非目标：不做数据库/ORM；不做跨主机同步；不做审计的远程投递。

## 2. 状态分类

### 2.1 运行时瞬态（仅内存，重启即清）

| 状态 | 载体 | 说明 |
| --- | --- | --- |
| 样本窗口（近期） | `Probe`/引擎内存 | 探针内部最近 N 次取数，用于趋势判定（B06） |
| 在途任务标志 | `HealthEngine._running`、`Scheduler.Job.task` | 重入保护与任务句柄（B03 §8、B10） |
| 冷却/熔断内存态 | `SafetyGuard` 内存镜像 | 与 `StateStore` 落盘副本同源，读时以落盘为准 |
| 本轮 `ProbeResult`/`Finding`、通知去重表 | `run_once` 局部变量、`Notifier._recent` | 巡检结束即丢弃；限流窗口（B08 §7） |

### 2.2 跨重启保留（落盘）

| 状态 | 键/文件 | 原因 |
| --- | --- | --- |
| 当前档位 / 守护模式 | `state.json: profile` / `mode` | 夜间档位与模式跨重启保持，启动时覆盖配置默认（B02 §8） |
| 夜间截止 | `state.json: night_until` | 到期回落 `normal`（B10 §6） |
| 各 target 冷却时间戳 / 熔断计数窗口 | `state.json: cooldown{}` / `circuit{}` | 防「重启容器即绕过冷却」（R1）；窗口内动作次数跨重启累计 |
| 事件历史 | 环形缓冲 → `state.json` 内联（元数据）+ `audit.jsonl`（全文） | `/guardian incidents` 与 `guardian_list_incidents` |
| 内存采样窗口 | `samples.json` | 泄漏趋势跨重启可继续（B06） |
| `last_status` 跃迁基线 | `state.json: last_status{}` | 重启后不重放全量告警（B08 §7） |

> **判据**：任何「重启后行为应保持一致」的状态必须落盘；任何「重启后重新计算即可」的状态不得落盘（避免写放大与陈旧数据）。

## 3. `core/state.py` 完整代码

```python
# core/state.py
"""Guardian 状态持久化：内存态 + 原子落盘 JSON + KV 镜像 + 事件环形缓冲（B00 D5/R6）。
跨重启保留 mode/profile/night_until/冷却/熔断/事件历史/采样窗口/last_status 基线；原子写（tmp +
os.replace）；schema 版本 + 迁移；坏文件备份 .corrupt.<ts> 后重建；脏标记 + 定期 flush；snapshot() 只读。
"""
from __future__ import annotations
import asyncio, json, os, time
from collections import deque
from pathlib import Path
from typing import Any
from astrbot.api import logger
SCHEMA_VERSION = 2
FLUSH_INTERVAL_SEC = 15.0
INCIDENT_RING = 200
KV_SCALARS = ("mode", "profile", "night_until")   # 少量关键标量镜像到 AstrBot KV
MAX_SAMPLE_POINTS = 512
class StateStore:
    def __init__(self, data_dir: Path, *, kv=None, ring: int = INCIDENT_RING,
                 flush_interval: float = FLUSH_INTERVAL_SEC) -> None:
        self.dir = Path(data_dir)
        self.state_path = self.dir / "state.json"
        self.samples_path = self.dir / "samples.json"
        self.kv = kv                       # AstrBot KV 适配器（get/put/delete），可为 None
        self.flush_interval = float(flush_interval)
        self._data: dict[str, Any] = self._defaults()
        self._incidents: deque[dict] = deque(maxlen=int(ring))
        self._samples: dict[str, list] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._flusher: asyncio.Task | None = None
    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "mode": "server", "profile": "normal", "night_until": None,
                "cooldown": {}, "circuit": {}, "last_status": {}, "seq": 0, "updated_ts": 0.0}
    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            self._load_json(self.state_path, "state")
            self._load_json(self.samples_path, "samples")
            await self._seed_from_kv_locked()
        self._flusher = asyncio.create_task(self._flush_loop(), name="guardian:state-flush")
        logger.info("[guardian] StateStore 就绪 path=%s schema=%d", self.state_path, SCHEMA_VERSION)
    async def stop(self) -> None:
        if self._flusher and not self._flusher.done():
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
        await self.save(force=True)
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)
    def set(self, key: str, value: Any) -> None:
        if self._data.get(key) != value:
            self._data[key] = value
            self._touch()
    def update(self, **kw: Any) -> None:
        if any(self._data.get(k) != v for k, v in kw.items()):
            self._data.update(kw)
            self._touch()
    def set_profile(self, profile: str) -> None:
        self.update(profile=str(profile))
    def cooldown_until(self, target: str) -> float:
        return float(self._data["cooldown"].get(target, 0.0))
    def set_cooldown(self, target: str, until_ts: float) -> None:
        self._data["cooldown"][target] = float(until_ts); self._touch()
    def circuit_window(self, target: str) -> list[float]:
        return list(self._data["circuit"].get(target, []))
    def set_circuit_window(self, target: str, stamps: list[float]) -> None:
        self._data["circuit"][target] = [float(s) for s in stamps]; self._touch()
    def push_sample(self, target: str, sample: dict) -> list[dict]:
        win = self._samples.setdefault(target, [])
        win.append(sample)
        if len(win) > MAX_SAMPLE_POINTS:
            del win[: len(win) - MAX_SAMPLE_POINTS]
        self._touch()
        return list(win)
    def samples(self, target: str) -> list[dict]:
        return list(self._samples.get(target, []))
    def record_incident(self, inc) -> None:
        self._incidents.append({"id": inc.id, "ts": inc.ts, "probe": inc.probe, "target": inc.target,
            "status": getattr(inc.status, "value", inc.status),
            "severity": getattr(inc.severity, "value", inc.severity), "message": inc.message,
            "suppressed": bool(inc.suppressed), "reason": getattr(inc, "suppress_reason", ""),
            "action": getattr(getattr(inc, "action", None), "kind", None)})
        self._touch()
    def recent_incidents(self, limit: int = 10) -> list[dict]:
        n = max(0, min(int(limit), self._incidents.maxlen or 0))
        return list(self._incidents)[-n:][::-1] if n else []
    def snapshot(self, scope: str = "all") -> dict[str, Any]:
        out = {"mode": self._data.get("mode"), "profile": self._data.get("profile"),
               "night_until": self._data.get("night_until"), "updated_ts": self._data.get("updated_ts"),
               "incident_count": len(self._incidents)}
        if scope in ("all", "containers"):
            out["last_status"] = dict(self._data.get("last_status", {}))
            out["cooldown"] = dict(self._data.get("cooldown", {}))
        if scope in ("all", "containers", "ros2"):
            out["sample_targets"] = sorted(self._samples)
        return out
    def _touch(self) -> None:
        self._data["seq"] = int(self._data.get("seq", 0)) + 1
        self._data["updated_ts"] = time.time()
        self._dirty = True
    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            if self._dirty:
                await self.save()
    async def save(self, force: bool = False) -> bool:
        if not (self._dirty or force):
            return True
        async with self._lock:
            state_txt = json.dumps(self._data, ensure_ascii=False, indent=2)
            samples_txt = json.dumps(self._samples, ensure_ascii=False)
            self._dirty = False
        ok1 = await asyncio.to_thread(self._atomic_write, self.state_path, state_txt)
        ok2 = await asyncio.to_thread(self._atomic_write, self.samples_path, samples_txt)
        if ok1 and ok2:
            await self._mirror_scalars_to_kv()
        return ok1 and ok2
    @staticmethod
    def _atomic_write(path: Path, text: str) -> bool:
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)          # 原子替换（R6）
            return True
        except OSError as exc:
            logger.error("[guardian] 状态落盘失败 path=%s: %s", path, exc)
            tmp.unlink(missing_ok=True)
            return False
    def _load_json(self, path: Path, kind: str) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, (dict, list)):
                raise ValueError("顶层不是对象/数组")
            if kind == "state":
                self._data = self._migrate(raw)
                self._incidents.clear()
            else:
                self._samples = {str(k): list(v) for k, v in dict(raw).items()}
            logger.info("[guardian] %s 已加载", path.name)
        except Exception as exc:  # noqa: BLE001 坏文件兜底
            backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            logger.error("[guardian] %s 损坏，已备份 %s 并重建: %s", path.name, backup.name, exc)
            if kind == "state":
                self._data = self._defaults()
                self._incidents.clear()
            else:
                self._samples = {}
            self._dirty = True
    def _migrate(self, raw: dict) -> dict:
        ver = int(raw.get("schema", 1))
        data = self._defaults()
        data.update({k: v for k, v in raw.items() if k in data})
        if ver < 2:                        # v1 -> v2：last_status 键统一为 "probe/target" 字符串
            data["last_status"] = {str(k): str(v) for k, v in (raw.get("last_status") or {}).items()}
            logger.info("[guardian] 状态迁移 v1 -> v2 完成")
        data["schema"] = SCHEMA_VERSION
        return data
    async def _seed_from_kv_locked(self) -> None:
        """文件缺失时用 KV 关键标量播种（首次启动或 data 卷被清空）。"""
        if self.state_path.exists() or self.kv is None:
            return
        for key in KV_SCALARS:
            try:
                val = await self.kv.get(key)
            except Exception:  # noqa: BLE001
                continue
            if val is not None:
                self._data[key] = val
        self._dirty = True
    async def _mirror_scalars_to_kv(self) -> None:
        if self.kv is None:
            return
        for key in KV_SCALARS:
            try:
                val = self._data.get(key)
                await (self.kv.delete(key) if val is None else self.kv.put(key, val))
            except Exception as exc:  # noqa: BLE001 KV 失败不影响文件主存
                logger.warning("[guardian] KV 镜像失败 key=%s: %s", key, exc)
```

## 4. 落盘路径设计

```
<StarTools.get_data_dir()>/            # 容器内通常 /AstrBot/data/plugin_data/astrbot_plugin_guardian
└── guardian/
    ├── state.json                  # 主状态：mode/profile/night_until/cooldown/circuit/last_status/seq
    ├── state.json.tmp.<pid>        # 原子写临时文件（瞬态，写后立即 os.replace）
    ├── state.json.corrupt.<ts>     # 损坏备份（保留待人工分析）
    ├── samples.json (+ .corrupt.<ts>)   # 内存采样窗口与损坏备份（B06）
    ├── audit.jsonl                 # 追加式审计（当前）
    ├── audit.jsonl.1 … .3          # 轮转备份（.1 最新，.3 最旧）
    └── memory_snapshots_<c>.json   # 重启前快照（B06，由动作层写入）
```

| 文件 | 写入者 | 读取者 | 生命周期 |
| --- | --- | --- | --- |
| `state.json` | `StateStore.save` | 启动加载、`snapshot` | 常驻，定期 + 退出时写 |
| `samples.json` | `StateStore.save` | `container_memory` 探针 | 常驻 |
| `audit.jsonl` | `AuditLog.append` | 运维复盘、`/guardian incidents --audit` | 追加，达 `max_bytes` 轮转 |
| `*.corrupt.<ts>` | 加载失败时 | 人工 | 永久保留，由运维清理 |

`StarTools.get_data_dir()` 失败时回退 `./data/plugin_data/<PLUGIN_NAME>`（B01 §8），并 `logger.warning`；两种路径均**不落在插件源码目录**（D5）。

## 5. `core/audit.py` 完整代码

```python
# core/audit.py
"""Guardian 审计日志：追加式 JSONL，不可关（B00 §10-7）。字段 ts/actor/trigger/probe/target/
action/params/result/ok/elapsed_ms/suppressed/reason；容量上限 + 轮转；敏感值脱敏；写入失败只告警。
"""
from __future__ import annotations
import asyncio, json, os, re, time
from pathlib import Path
from typing import Any
from astrbot.api import logger
REDACT_KEYS = ("token", "access_token", "password", "secret", "authorization")
REDACT_PAT = re.compile(r"(?i)(token|access_token|password|secret)=([^&\s]+)")
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 3
def redact(value: Any) -> Any:
    """递归脱敏：命中敏感键的值替换为 ***；字符串中的 k=v 形式亦替换。"""
    if isinstance(value, dict):
        return {k: ("***" if str(k).lower() in REDACT_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return REDACT_PAT.sub(lambda m: f"{m.group(1)}=***", value) if isinstance(value, str) else value
class AuditLog:
    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 backups: int = DEFAULT_BACKUPS) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self._lock = asyncio.Lock()
    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
    async def append(self, incident=None, *, kind: str = "", target: str = "",
                     detail: str = "", incident_id: str = "", actor: str = "system",
                     trigger: str = "engine", ok: bool = True, elapsed_ms: int = 0,
                     **extra: Any) -> None:
        """append(incident) 记录 Incident；append(kind=..., target=..., detail=...) 记录动作（兼容 B03/B08）。"""
        rec = self._from_incident(incident) if incident is not None else {}
        if kind:
            rec["kind"] = kind
        for k, v in (("target", target), ("detail", detail), ("incident_id", incident_id)):
            if v not in ("", None):
                rec[k] = v
        if incident is None:
            rec["ok"] = bool(ok)
            rec["elapsed_ms"] = int(elapsed_ms)
        rec.update(extra)
        rec.setdefault("ts", time.time())
        rec.setdefault("actor", actor)
        rec.setdefault("trigger", trigger)
        line = json.dumps(redact(rec), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            try:
                await self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:            # 审计失败不拖垮业务（B00 §10）
                logger.error("[guardian] 审计写入失败 path=%s: %s | %s", self.path, exc, line[:200])
    @staticmethod
    def _from_incident(inc) -> dict:
        res = getattr(inc, "result", None)
        return {"kind": "incident.recorded", "incident_id": inc.id, "ts": inc.ts, "probe": inc.probe,
                "target": inc.target, "status": getattr(inc.status, "value", inc.status),
                "severity": getattr(inc.severity, "value", inc.severity),
                "action": getattr(getattr(inc, "action", None), "kind", None),
                "params": getattr(getattr(inc, "action", None), "params", None),
                "result": getattr(res, "detail", None), "ok": bool(res.ok) if res is not None else True,
                "elapsed_ms": int(getattr(res, "elapsed_ms", 0) or 0),
                "suppressed": bool(getattr(inc, "suppressed", False)),
                "reason": getattr(inc, "suppress_reason", "") or inc.message}
    async def _rotate_if_needed(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        logger.info("[guardian] 审计日志轮转完成，保留 %d 份", self.backups)
```

> 兼容性：B03 §6 的 `deps["audit"].append(inc)` 与 B08 §4 的 `audit.append(kind=..., target=..., detail=..., incident_id=...)` 均由同一 `append` 支持；前者走 `_from_incident`，后者走关键字分支。所有动作（含被抑制）都必须写审计（B00 §10-7）。

## 6. 事件历史（环形缓冲）

| 项 | 设计 |
| --- | --- |
| 容器 | `collections.deque(maxlen=N)`，N 默认 `200`（可配 `state.incident_ring`） |
| 记录时机 | `HealthEngine._record` → `state.record_incident(inc)`（B03 §6） |
| 记录内容 | id/ts/probe/target/status/severity/message/suppressed/reason/action.kind（**不含** raw 与凭据） |
| 查询/消费方 | `recent_incidents(limit)` 返回**倒序**（最新在前），`limit ∈ [1,50]`；供 `/guardian incidents [n]` 与 `guardian_list_incidents` |
| 持久化 | 内存环形缓冲为**权威**；审计 `audit.jsonl` 保留全文可回溯；`state.json` 不内联历史（避免主状态膨胀） |

**静默 ≠ 失明**：被档位静默或限流抑制的事件仍写入缓冲与审计（`suppressed=True` + `reason`），查询时可完整复盘（B08 §8）。

## 7. KV 与文件的取舍

| 维度 | AstrBot KV（`get_kv_data`/`put_kv_data`） | 文件（`data/guardian/*.json*`） |
| --- | --- | --- |
| 适合 / 写入成本 | 少量关键标量：`mode`/`profile`/`night_until`；框架层落盘，粒度粗 | 大结构：冷却表、熔断窗口、采样窗口、事件历史；原子写单文件，可控 |
| 查询 | 单键读，无遍历语义 | 可整体加载与迁移 |
| 权威性 | **镜像**（缓存） | **权威（source of truth）** |
| 使用位置 | 启动播种、跨插件读 | 全部运行时读写 |

**收敛规则**：① 正常路径只写文件，`save()` 成功后把 `KV_SCALARS` 镜像到 KV；② 启动时若文件缺失而 KV 有值，用 KV 播种（首次部署或 data 卷被清空）；③ 两者不一致时**文件优先**，KV 被下一次 `save()` 覆盖修正；④ KV 读写异常仅告警，不影响文件主存。

## 8. 数据一致性

**写入顺序**（一次状态变更）：

```
业务改动 state.set/update（内存）
  → _touch()（seq+1, updated_ts=now, dirty=true）
  → [定期 flush 或 stop(force)] save()
       → 序列化 state.json / samples.json（持锁，快照内存视图）
       → 原子写：tmp.<pid> → os.replace（各自独立）
       → 成功则镜像 KV 标量
```

**崩溃点分析**：

| 崩溃点 | 后果 | 兜底 |
| --- | --- | --- |
| `_touch` 之后、`save` 之前 | 内存改动丢失（最多 `flush_interval` 秒） | 关键动作（切档位/夜间/动作后）**立即 `save()`**，不等定时 flush |
| 写 `tmp` 中途 | 临时文件不完整 | 下次启动忽略 `*.tmp.*`，权威文件未变 |
| `os.replace` 中途 | 由文件系统保证原子性 | 旧文件或新文件二者其一，绝无半截 |
| 两个文件之间（state 成功、samples 失败） / `os.replace` 后、KV 镜像前 | 两份文件 `seq` 不一致 / KV 陈旧 | 各自独立加载，samples 允许落后，下次 `save` 补齐；启动播种仅用于文件缺失场景，不影响权威 |

**幂等重放**：`save()` 为幂等操作，重复调用只产生相同内容；`_touch` 用 `seq` 单调递增标识版本，便于排查「哪次写入落后」。

**双写不一致收敛**：文件与 KV 不一致时以文件为准；`_seed_from_kv_locked` **仅当文件不存在**时才读取 KV，杜绝「KV 旧值覆盖文件新值」。

## 9. 隐私与脱敏

| 数据 | 存储策略 | 展示规则 |
| --- | --- | --- |
| `user_id` / `sender_id` | 审计 `actor` 记为 `u:***XXXX`（末 4 位） | 命令/工具回执不显示完整 ID |
| UMO | 审计 `target` 记为 `umo:***<sha1[:8]>` | 完整 UMO 仅存于配置 `notify.umo_list`，不写入审计 |
| `qq.access_token` | 配置存储，**不落** state/audit | 回执与日志一律 `***` |
| 探针 `raw` 原始响应 / 消息内容 `message` | 截断 2000 字符后进 `Incident.raw`（**不写** state/audit）；`message` 保留用于复盘 | 诊断时按需展示且凭据被 `redact`；`message` 不含凭据，保持原样 |

**统一脱敏入口**：`core/audit.py:redact()` 递归处理 dict/list/str，键名命中 `REDACT_KEYS` 或字符串匹配 `k=v` 模式即替换为 `***`；任何新增字段都应经过它。

## 10. 备份与恢复

**备份（手动拷贝）**：

```bash
docker exec astrbot tar czf /tmp/guardian-backup.tgz -C /AstrBot/data/plugin_data/astrbot_plugin_guardian guardian
docker cp astrbot:/tmp/guardian-backup.tgz ./guardian-backup-$(date +%F).tgz   # 或直接拷贝宿主挂载目录 ./data/astrbot/data
```

**恢复步骤**：

1. `docker compose stop astrbot`（避免运行中覆盖）。
2. 解包到 `data/guardian/`，**保持 `state.json` 的 `schema` 字段**不变。
3. 删除残留 `*.tmp.*`；如需保留现场，先重命名 `*.corrupt.*` 而非删除。
4. `docker compose up -d astrbot`，观察日志 `[guardian] StateStore 就绪`。
5. 校验：`/guardian status` 的 `profile/mode/night_until` 与备份一致；`/guardian incidents 5` 有历史。

**版本迁移注意**：备份跨版本恢复时，`_migrate()` 会按 `schema` 逐级升级；**不可**用旧版插件加载新版 `state.json`（字段可能被忽略）。降级前先备份并删除高版本专有字段。

## 11. 与 AstrBot 插件规范一致性

| 规范 | 落实 |
| --- | --- |
| 数据必须存 `data` 目录 | 统一走 `StarTools.get_data_dir()/guardian/`（D5） |
| 插件更新/重装不丢数据 | 源码与数据目录分离（`plugins/` vs `data/`，B01 §5、B13） |
| 热重载不残留 / 启动幂等 | `terminate()` → `state.stop()` 强制落盘并取消 flush 任务（B10 §7、R10）；`start()` 先加载后建 flush 任务，重复调用不产生重复任务 |
| 不阻塞事件循环 | 文件写走 `asyncio.to_thread`，无同步 IO 阻塞（D9） |
| 少依赖 | 仅标准库 `asyncio/json/os/time/collections/re`，无第三方（ADR D7 精神） |

**KV 与 `get_kv_data` 的关系**：`StateStore.kv` 为适配器（包装插件的 `get_kv_data/put_kv_data/delete_kv_data`），仅镜像 `mode/profile/night_until`；其余状态一律走文件，避免 KV 被大结构拖慢。

## 12. 验收标准（对应 C11）

| 编号 | 判据 | 方法 | 关联 |
| --- | --- | --- | --- |
| C11-1 | `profile`/`mode`/`night_until`/`cooldown`/`circuit` 重启后保持，冷却不被绕过 | 改值 → `stop` → 重启加载；设冷却后动作仍被抑制 | S-G1 / R1 |
| C11-2 | 关键动作后立即落盘；定时 flush 仅在脏标记时写 | 切档位后立刻 kill 再启动；观测 `updated_ts`/mtime | §8 |
| C11-3 | 半截 JSON 或非对象顶层触发 `.corrupt.<ts>` 备份并重建、进程不崩；写 `tmp` 中断不影响权威文件 | 注入截断/非法内容或残留 `tmp` 后启动 | S-G2 / R6 |
| C11-4 | 旧 schema（v1）加载后迁移到 v2 且数据保留 | 放入 v1 文件启动 | S-G2 |
| C11-5 | `samples.json` 独立落盘/加载，`MAX_SAMPLE_POINTS` 上限生效 | 超上限后截断 | S-G1 |
| C11-6 | 每次动作（含被抑制）写审计；达 `max_bytes` 轮转保留 `backups` 份；`append` 两种形态均可 | 触发 deny 路径后查 `audit.jsonl`；写满查 `.1/.2/.3` | S-G4 / B00 §10-7 |
| C11-7 | `recent_incidents(N)` 返回最近 N 条倒序，含 suppressed | 造 5 条事件后查询 | S-G5 |
| C11-8 | 文件为权威：文件存在时忽略 KV 旧值；文件缺失时由 KV 播种 | 制造不一致/删除文件后启动 | S-G6 |
| C11-9 | 审计脱敏：`access_token`/`token` 均显示为 `***` | 构造含凭据的 params | S-G4 |
| C11-10 | 数据目录位于 `StarTools.get_data_dir()`、插件目录无写入；`stop()` 后无存活 `guardian:state-flush` 任务 | 检查路径与文件树；`terminate` 后枚举任务 | S-G6 / D5 / R10 |
| C11-11 | 落盘失败（只读目录）仅告警，业务不中断 | 挂只读卷测试 | S-G2 |

**Gate 复述**：`G-SAFETY` 要求冷却/熔断跨重启仍生效（C11-2）；`G-STABILITY` 要求 72h 无状态膨胀、无因坏文件启动失败（C11-5/6/18）。通过 C11 即达成里程碑 M2（引擎）的持久化部分，为 B04–B07 的动作冷却与 B08 的跃迁基线提供存储底座。
