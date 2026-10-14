"""Guardian 守护系统 —— 异步调度器与 cron-lite。

对应文档：B10

支持三类任务：
- interval：固定间隔（带 jitter）
- cron：5 字段标准 cron（* , - / 与列表、步长；dow 0-7）
- once：一次性延时

设计为单循环 tick（默认 1s），任务异常隔离，shutdown 严格取消（R10）。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .log import logger

JobFunc = Callable[[], Awaitable[Any] | Any]

# --------------------------------------------------------------------------- #
# cron 解析（纯函数，可单测）
# --------------------------------------------------------------------------- #
class CronError(ValueError):
    pass


def _parse_field(expr: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronError(f"步长非法: {part}") from exc
            if step <= 0:
                raise CronError(f"步长必须为正: {part}")
            part = base.strip()
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError as exc:
                raise CronError(f"区间非法: {part}") from exc
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise CronError(f"字段非法: {part}") from exc
        if start > end:
            start, end = end, start
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                values.add(v)
    if not values:
        raise CronError(f"字段无有效取值: {expr}")
    return values


@dataclass
class CronExpr:
    minute: set[int]
    hour: set[int]
    dom: set[int]
    month: set[int]
    dow: set[int]
    raw: str = ""
    dom_restricted: bool = False
    dow_restricted: bool = False


def parse_cron(expr: str) -> CronExpr:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise CronError(f"cron 需 5 个字段，得到 {len(parts)}: {expr!r}")
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow_raw = _parse_field(parts[4], 0, 7)
    dow = {0 if v == 7 else v for v in dow_raw}
    return CronExpr(
        minute=minute, hour=hour, dom=dom, month=month, dow=dow, raw=expr,
        dom_restricted=parts[2].strip() not in ("*", "?"),
        dow_restricted=parts[4].strip() not in ("*", "?"),
    )


def cron_match(expr: str | CronExpr, dt: datetime) -> bool:
    """判断 dt（本地时间）是否命中 cron。"""
    ce = parse_cron(expr) if isinstance(expr, str) else expr
    if dt.minute not in ce.minute:
        return False
    if dt.hour not in ce.hour:
        return False
    if dt.month not in ce.month:
        return False
    # Python: Monday=0..Sunday=6 -> cron: Sunday=0..Saturday=6
    cron_dow = (dt.weekday() + 1) % 7
    dom_ok = dt.day in ce.dom
    dow_ok = cron_dow in ce.dow
    if ce.dom_restricted and ce.dow_restricted:
        return dom_ok or dow_ok  # 标准 cron 的 OR 语义
    if ce.dom_restricted:
        return dom_ok
    if ce.dow_restricted:
        return dow_ok
    return True


def next_run(expr: str | CronExpr, after: datetime | None = None,
             max_days: int = 400) -> datetime | None:
    """返回 after 之后的第一个命中时刻（分钟精度）。"""
    ce = parse_cron(expr) if isinstance(expr, str) else expr
    cur = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cur + timedelta(days=max_days)
    while cur < limit:
        if cron_match(ce, cur):
            return cur
        # 快速跳过：小时不匹配则跳到下一小时
        if cur.hour not in ce.hour or cur.month not in ce.month:
            cur = (cur + timedelta(hours=1)).replace(minute=0)
        else:
            cur = cur + timedelta(minutes=1)
    return None


# --------------------------------------------------------------------------- #
# 任务与调度器
# --------------------------------------------------------------------------- #
@dataclass
class ScheduledJob:
    name: str
    func: JobFunc
    kind: str = "interval"          # interval | cron | once
    interval: float = 60.0
    jitter: float = 0.0
    cron: str = ""
    next_run: float = 0.0
    run_at: float = 0.0
    runs: int = 0
    failures: int = 0
    last_error: str = ""
    enabled: bool = True
    _cron_expr: CronExpr | None = None

    def schedule_next(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.kind == "interval":
            jitter = random.uniform(0, self.jitter) if self.jitter > 0 else 0.0
            self.next_run = now + self.interval + jitter
        elif self.kind == "cron" and self._cron_expr is not None:
            nxt = next_run(self._cron_expr, datetime.now())
            self.next_run = nxt.timestamp() if nxt else now + 86400
        elif self.kind == "once":
            self.next_run = self.run_at

    def describe(self) -> str:
        if self.kind == "cron":
            return f"{self.name}[cron {self.cron}] runs={self.runs} fail={self.failures}"
        if self.kind == "once":
            return f"{self.name}[once @{time.strftime('%H:%M:%S', time.localtime(self.run_at))}]"
        return (f"{self.name}[every {self.interval:.0f}s] runs={self.runs} "
                f"fail={self.failures}")


class Scheduler:
    """单循环调度器。所有任务在同一事件循环中串行触发（各自独立 await）。"""

    def __init__(self, tick: float = 1.0) -> None:
        self._jobs: dict[str, ScheduledJob] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._tick = tick
        self._busy = False

    # ---- 注册 ---- #
    def add_interval_job(self, name: str, func: JobFunc, interval: float,
                         jitter: float = 0.0) -> ScheduledJob:
        job = ScheduledJob(name=name, func=func, kind="interval",
                           interval=max(1.0, float(interval)), jitter=float(jitter))
        job.schedule_next()
        self._jobs[name] = job
        return job

    def add_cron_job(self, name: str, func: JobFunc, expr: str) -> ScheduledJob:
        ce = parse_cron(expr)  # 非法 cron 在此抛出，由调用方兜底
        job = ScheduledJob(name=name, func=func, kind="cron", cron=expr, _cron_expr=ce)
        job.schedule_next()
        self._jobs[name] = job
        return job

    def add_once_job(self, name: str, func: JobFunc, delay_sec: float) -> ScheduledJob:
        job = ScheduledJob(name=name, func=func, kind="once",
                           run_at=time.time() + max(0.0, float(delay_sec)))
        job.schedule_next()
        self._jobs[name] = job
        return job

    def cancel(self, name: str) -> None:
        self._jobs.pop(name, None)

    def cancel_prefix(self, prefix: str) -> None:
        for name in [n for n in self._jobs if n.startswith(prefix)]:
            self._jobs.pop(name, None)

    def get(self, name: str) -> ScheduledJob | None:
        return self._jobs.get(name)

    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    def clear(self) -> None:
        self._jobs.clear()

    # ---- 生命周期 ---- #
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="guardian:scheduler")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick)
                return  # 被 stop
            except asyncio.TimeoutError:
                pass
            await self._run_due()

    async def _run_due(self) -> None:
        now = time.time()
        for job in list(self._jobs.values()):
            if not job.enabled or job.next_run > now:
                continue
            await self._invoke(job)

    async def _invoke(self, job: ScheduledJob) -> None:
        try:
            result = job.func()
            if asyncio.iscoroutine(result):
                await result
            job.runs += 1
            job.last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            job.failures += 1
            job.last_error = str(exc)
            logger.warning(f"[guardian] 任务 {job.name} 执行失败: {exc}")
        finally:
            if job.kind == "once":
                self._jobs.pop(job.name, None)
            else:
                job.schedule_next()

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self.clear()

    def next_runs_text(self) -> str:
        rows = []
        for job in self.jobs():
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(job.next_run))
            rows.append(f"  - {job.name}: 下次 {when} ({job.kind})")
        return "\n".join(rows) if rows else "  (无任务)"
