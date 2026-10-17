"""Guardian 守护系统 —— 独立内核进程服务（B16 §9 M3）。

```bash
python -m astrbot_plugin_guardian.kernel                 # 常驻
python -m astrbot_plugin_guardian.kernel --selftest      # 起→自 ping→跑一轮→退出
python -m astrbot_plugin_guardian.kernel --print-config  # 只打印解析后的配置
```

**配置从哪来？** 独立进程没有 `AstrBotConfig`，所以按优先级取：

1. `--config-file <path>` / `GUARDIAN_CONFIG_FILE`：JSON 文件
2. `GUARDIAN_CONFIG_JSON`：内联 JSON 字符串（适合 compose `environment`）
3. 都没有 → 空配置（全部走 DEFAULTS）

**数据目录**：`--data-dir` / `GUARDIAN_DATA_DIR`，否则用 `kernel.shared_dir`，
再否则用内核自有的 `default_data_dir()`。

本模块不得 `import astrbot`（I6）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
from typing import Any

from ..core.config import build_config
from ..core.log import logger
from ..ipc.server import KernelIpcServer
from ..kernel_runtime import KERNEL_VERSION, GuardianKernel, default_data_dir

CONFIG_FILE_ENV = "GUARDIAN_CONFIG_FILE"
CONFIG_JSON_ENV = "GUARDIAN_CONFIG_JSON"


def load_raw_config(*, config_file: str = "", inline: str = "") -> dict[str, Any]:
    """按优先级解析原始配置；任何一步失败都**降级而不是崩**（坏配置必须能启动）。"""
    path = (config_file or os.environ.get(CONFIG_FILE_ENV) or "").strip()
    if path:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
            logger.warning(f"[guardian] 配置文件 {path} 顶层不是对象，已忽略")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[guardian] 读取配置文件 {path} 失败: {exc}")
    blob = (inline or os.environ.get(CONFIG_JSON_ENV) or "").strip()
    if blob:
        try:
            data = json.loads(blob)
            if isinstance(data, dict):
                return data
            logger.warning("[guardian] GUARDIAN_CONFIG_JSON 顶层不是对象，已忽略")
        except json.JSONDecodeError as exc:
            logger.warning(f"[guardian] 解析 GUARDIAN_CONFIG_JSON 失败: {exc}")
    return {}


class KernelService:
    """把"内核 + IPC 服务端"装配成一个可常驻、可优雅退出的服务。"""

    def __init__(self, raw_config: dict | None = None, *,
                 data_dir: str = "") -> None:
        self.raw_config = dict(raw_config or {})
        self.config = build_config(self.raw_config, data_dir=data_dir)
        kc = self.config.kernel
        self.data_dir = data_dir or str(kc.shared_dir or "") or default_data_dir()
        self.shared_dir = str(kc.shared_dir or "") or self.data_dir
        self.kernel = GuardianKernel(self.raw_config, data_dir=self.data_dir)
        self.server = KernelIpcServer(
            self.kernel,
            transport=str(kc.transport or "tcp"),
            socket_path=str(kc.socket_path or ""),
            tcp_host=str(kc.tcp_host or "127.0.0.1"),
            tcp_port=int(kc.tcp_port or 6198),
            token_file=str(kc.token_file or ""),
            shared_dir=self.shared_dir,
            heartbeat_interval=float(kc.heartbeat_interval or 30),
            version=KERNEL_VERSION,
        )
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        # 配置归一化必须**说出来**：静默改写配置会让运维对不上账。
        for issue in self.config.issues:
            logger.warning(f"[guardian] 内核配置归一化：{issue}")
        if not self.config.enabled:
            logger.warning("[guardian] enabled=false：内核不启动巡检，仅保持 IPC 存活")
        await self.server.start()
        if self.config.enabled:
            await self.kernel.start()
        logger.info(
            f"[guardian] 独立内核已就绪 | version={KERNEL_VERSION} "
            f"endpoint={self.server.endpoint} data_dir={self.data_dir}")
        await self._stopping.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("[guardian] 独立内核开始退出")
        with contextlib.suppress(Exception):
            await self.kernel.stop()
        with contextlib.suppress(Exception):
            await self.server.stop()
        logger.info("[guardian] 独立内核已退出")

    def request_stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------------ #
    async def selftest(self) -> int:
        """起 → 自 ping → 跑一轮巡检 → 退出。用于 M3 的部署期冒烟验证。"""
        await self.server.start()
        await self.kernel.start()
        from ..ipc.client import KernelIpcClient

        client = KernelIpcClient(
            transport="tcp" if self.server.transport == "tcp" else "unix",
            socket_path=self.server.socket_path,
            tcp_host=self.server.tcp_host,
            tcp_port=self.server.bound_port,
            token_file=self.server.token_file,
            shared_dir=self.shared_dir,
            request_timeout=15,
        )
        rc = 0
        try:
            await client.connect()
            pong = await client.call("ping", {})
            logger.info(f"[guardian] selftest ping -> {pong}")
            result = await client.call("run_check", {"scope": "all"})
            n = len(result.get("results") or [])
            logger.info(f"[guardian] selftest patrol -> {n} 项探针结果")
            if n == 0:
                logger.error("[guardian] selftest 失败：巡检没有产出任何结果")
                rc = 2
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[guardian] selftest 失败: {exc}")
            rc = 1
        finally:
            with contextlib.suppress(Exception):
                await client.close()
            await self.shutdown()
        return rc


def _install_signal_handlers(service: KernelService) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, service.request_stop)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m astrbot_plugin_guardian.kernel",
        description="Guardian 独立内核服务（B16 §9 M3）")
    parser.add_argument("--config-file", default="",
                        help=f"JSON 配置文件路径（或环境变量 {CONFIG_FILE_ENV}）")
    parser.add_argument("--config-json", default="",
                        help=f"内联 JSON 配置（或环境变量 {CONFIG_JSON_ENV}）")
    parser.add_argument("--data-dir", default="",
                        help="内核数据目录（或环境变量 GUARDIAN_DATA_DIR）")
    parser.add_argument("--selftest", action="store_true",
                        help="起服务 → 自 ping → 跑一轮巡检 → 退出")
    parser.add_argument("--print-config", action="store_true",
                        help="打印解析后的配置（含归一化 issues）后退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    raw = load_raw_config(config_file=args.config_file, inline=args.config_json)
    data_dir = args.data_dir or os.environ.get("GUARDIAN_DATA_DIR") or ""

    if args.print_config:
        cfg = build_config(raw, data_dir=data_dir)
        print(json.dumps({"config": cfg.raw, "issues": cfg.issues},
                         ensure_ascii=False, indent=2))
        return 0

    service = KernelService(raw, data_dir=data_dir)
    if args.selftest:
        return asyncio.run(service.selftest())

    async def _run() -> None:
        _install_signal_handlers(service)
        await service.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    return 0


__all__ = ["KernelService", "load_raw_config", "main"]
