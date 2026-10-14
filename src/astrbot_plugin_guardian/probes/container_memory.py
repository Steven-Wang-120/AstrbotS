"""容器内存：Docker CLI working-set 口径与低频、连续确认的趋势检测。"""
from __future__ import annotations

import math
import time
from typing import Any

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.dockerclient import DockerError, DockerUnavailable
from .base import Probe

MIB = 1024 ** 2
SAMPLE_MAX_AGE_SEC = 6 * 3600
SAMPLE_VERSION = "docker-cli-working-set-v2"


def compute_slope_mb_per_hour(samples: list[list[float]]) -> float:
    """最小二乘斜率，时间戳 0 也是合法样本。"""
    pts = [(float(t), float(mb)) for t, mb in samples if t is not None and mb is not None]
    n = len(pts)
    if n < 3 or pts[-1][0] <= pts[0][0]:
        return 0.0
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((p[0] - mx) ** 2 for p in pts)
    if denom <= 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / denom * 3600.0


def memory_working_set(mem: dict) -> tuple[float, float, str]:
    """与 Docker CLI 一致：v1 total_inactive_file / v2 inactive_file。

    缺少 inactive 字段的旧引擎回退 cache；绝不把 rss 当作缓存扣除。
    字段存在且为 0 时仍有效，避免 `or` 回退切换口径。
    """
    usage = float(mem["usage"])
    if not math.isfinite(usage) or usage < 0:
        raise ValueError("invalid memory usage")
    stats = mem.get("stats") or {}
    cache, source = 0.0, "usage"
    for key in ("total_inactive_file", "inactive_file", "cache"):
        if key in stats and stats[key] is not None:
            candidate = float(stats[key])
            if 0 <= candidate < usage:
                cache, source = candidate, f"usage-{key}"
            break
    return max(0.0, usage - cache), cache, source


@register_probe("container_memory")
class ContainerMemoryProbe(Probe):
    name = "container_memory"
    scope = "server"
    timeout = 12.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return list(targets) if targets else list(config.all_containers)

    def _reset_confirmation(self) -> None:
        meta_all = dict(self.state.get("memory.sample_meta") or {})
        meta = dict(meta_all.get(self.target) or {})
        meta["leak_streak"] = 0
        meta_all[self.target] = meta
        self.state.set("memory.sample_meta", meta_all)

    async def check(self):
        result = await super().check()
        if result.status == Status.UNKNOWN:
            self._reset_confirmation()
        return result

    async def _check(self):
        target = self.target
        try:
            raw = await self.docker.stats(target)
        except (DockerError, DockerUnavailable) as exc:
            self._reset_confirmation()
            return self.result(Status.UNKNOWN, Severity.WARN,
                               f"内存采样失败：{exc}", {"stats_error": True})
        mem = raw.get("memory_stats") or {}
        if "usage" not in mem:
            self._reset_confirmation()
            return self.result(Status.UNKNOWN, Severity.WARN,
                               "Docker 未返回有效内存统计", {"stats_error": True})
        effective, cache, source = memory_working_set(mem)
        if not math.isfinite(effective):
            self._reset_confirmation()
            return self.result(Status.UNKNOWN, Severity.WARN, "内存统计不是有限数值")
        mem_mb = effective / MIB
        usage = float(mem["usage"])
        limit = float(mem.get("limit") or 0.0)
        stats = mem.get("stats") or {}

        # 样本只属于同一容器实例；跨容器重启、口径升级的旧样本不能混合回归。
        try:
            info = await self.docker.inspect(target)
            started_at = str((info.get("State") or {}).get("StartedAt") or "")
        except (DockerError, DockerUnavailable):
            self._reset_confirmation()
            return self.result(Status.UNKNOWN, Severity.WARN,
                               "无法确认容器实例，暂不进行内存趋势判定",
                               {"mem_mb": round(mem_mb, 2), "stats_error": True})
        if not started_at:
            self._reset_confirmation()
            return self.result(Status.UNKNOWN, Severity.WARN,
                               "缺少容器启动时间，暂不进行内存趋势判定",
                               {"mem_mb": round(mem_mb, 2), "stats_error": True})

        cfg = self.config.memory
        now = time.time()
        samples_all: dict[str, Any] = dict(self.state.get("samples") or {})
        meta_all = dict(self.state.get("memory.sample_meta") or {})
        meta = dict(meta_all.get(target) or {})
        identity = [SAMPLE_VERSION, started_at, source]
        window = samples_all.get(target) or []
        if meta.get("identity") != identity or (window and window[-1][0] > now):
            window, meta = [], {"identity": identity, "leak_streak": 0}
        cleaned = []
        for point in window:
            try:
                ts, value = map(float, point)
                if math.isfinite(ts) and math.isfinite(value) and 0 <= now - ts <= SAMPLE_MAX_AGE_SEC:
                    cleaned.append([ts, value])
            except (TypeError, ValueError):
                continue
        window = sorted(cleaned)
        new_sample = not window or now - window[-1][0] >= cfg.sample_interval
        if new_sample:
            window.append([now, mem_mb])
        window = window[-cfg.samples:]
        span = window[-1][0] - window[0][0] if len(window) > 1 else 0.0
        growth = compute_slope_mb_per_hour(window)
        net_growth = window[-1][1] - window[0][1] if window else 0.0
        candidate = (len(window) >= 5 and span >= cfg.leak_min_span_sec
                     and growth >= cfg.growth_warn_mb_per_hour
                     and net_growth >= cfg.leak_min_growth_mb)
        if new_sample:
            meta["leak_streak"] = int(meta.get("leak_streak") or 0) + 1 if candidate else 0
        leak = candidate and int(meta.get("leak_streak") or 0) >= cfg.leak_confirm_samples
        samples_all[target] = window
        meta_all[target] = meta
        self.state.set("samples", samples_all)
        self.state.set("memory.sample_meta", meta_all)
        metrics = {
            "mem_mb": round(mem_mb, 2),
            "mem_peak_mb": round(max(p[1] for p in window), 2),
            "mem_avg_mb": round(sum(p[1] for p in window) / len(window), 2),
            "mem_limit_mb": round(limit / MIB, 2),
            "mem_raw_mb": round(usage / MIB, 2),
            "cache_mb": round(cache / MIB, 2),
            "anon_mb": round(float(stats["anon"]) / MIB, 2) if "anon" in stats else None,
            "memory_source": source,
            "growth_mb_per_hour": round(growth, 2),
            "net_growth_mb": round(net_growth, 2),
            "span_sec": round(span, 1),
            "samples": len(window), "new_sample": new_sample,
            "leak_streak": int(meta.get("leak_streak") or 0),
            "leak": bool(leak),
        }
        if mem_mb >= cfg.crit_mb:
            return self.result(Status.CRIT, Severity.CRIT,
                               f"{target} 内存 {mem_mb:.0f}MiB 超过严重阈值 {cfg.crit_mb}MiB", metrics)
        if mem_mb >= cfg.warn_mb:
            return self.result(Status.WARN, Severity.WARN,
                               f"{target} 内存 {mem_mb:.0f}MiB 超过告警阈值 {cfg.warn_mb}MiB", metrics)
        if leak:
            return self.result(Status.WARN, Severity.WARN,
                               f"{target} 疑似内存泄漏：增长 {growth:.1f}MiB/h，"
                               f"窗口 {span / 60:.0f}min 净增 {net_growth:.1f}MiB，"
                               f"连续 {meta['leak_streak']} 个新样本确认", metrics)
        return self.result(Status.OK, Severity.INFO,
                           f"{target} 内存 {mem_mb:.0f}MiB（增长 {growth:.1f}MiB/h，"
                           f"趋势窗口 {span / 60:.0f}min）", metrics)
