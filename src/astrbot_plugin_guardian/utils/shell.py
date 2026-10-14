"""Guardian 守护系统 —— 异步子进程执行（绝不使用 shell）。

对应文档：B07 §3、B12 §命令注入防护
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from astrbot.api import logger


@dataclass
class CmdResult:
    rc: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def text(self) -> str:
        return (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")


async def run_argv(argv: list[str], timeout: float = 15.0,
                   env: dict[str, str] | None = None) -> CmdResult:
    """执行 argv 列表（不使用 shell=True），带超时。

    argv 必须已由调用方通过白名单校验；本函数不做安全判断，只负责执行。
    """
    if not argv:
        return CmdResult(rc=-1, stderr="空命令")
    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        return CmdResult(rc=127, stderr=f"命令不存在: {exc}",
                         elapsed_ms=int((time.time() - started) * 1000))
    except OSError as exc:
        return CmdResult(rc=126, stderr=f"无法启动命令: {exc}",
                         elapsed_ms=int((time.time() - started) * 1000))
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return CmdResult(rc=124, timed_out=True,
                         stderr=f"命令超时（{timeout}s）",
                         elapsed_ms=int((time.time() - started) * 1000))
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    elapsed = int((time.time() - started) * 1000)
    result = CmdResult(
        rc=proc.returncode or 0,
        stdout=stdout_b.decode("utf-8", "replace") if stdout_b else "",
        stderr=stderr_b.decode("utf-8", "replace") if stderr_b else "",
        elapsed_ms=elapsed,
    )
    if not result.ok:
        logger.debug(f"[guardian] 命令失败 rc={result.rc}: {' '.join(argv[:3])}...")
    return result
