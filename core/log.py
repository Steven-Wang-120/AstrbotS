"""Guardian 日志门面 —— 内核脱离 AstrBot 独立运行的前提。

对应文档：B16（独立内核服务架构）

独立内核服务必须在 AstrBot **进程之外**运行，否则它会在睡眠/重启本体容器时
把自己一起杀死。因此内核侧模块（`core/`、`probes/`、`utils/`、`actions/`、
`notify/`、`modes/`）一律**不得**直接 `import astrbot`。

本模块是这些模块唯一的日志出口：

- **在 AstrBot 内运行时**：直接转发给 `astrbot.api.logger`，日志行为、格式、
  级别与既有一致（不改变插件内的日志表现）；
- **在独立内核进程内运行时**：退化为标准库 `logging`，由内核入口配置输出到
  stderr / journald。

AstrBot 适配层（`main.py`、`runtime.py`）与 LLM 工具层（`tools/`）不受此约束。
"""

from __future__ import annotations

import logging

FALLBACK_LOGGER_NAME = "guardian.kernel"

_REQUIRED_METHODS = ("debug", "info", "warning", "error", "critical", "exception")


def _resolve_host_logger():
    """尽力取到 AstrBot 的 logger；取不到或接口不符则返回 None。"""
    try:
        from astrbot.api import logger as host_logger  # type: ignore
    except Exception:  # noqa: BLE001 - astrbot 缺失、导入期异常或 API 变更
        return None
    if host_logger is None:
        return None
    for name in _REQUIRED_METHODS:
        if not callable(getattr(host_logger, name, None)):
            return None
    return host_logger


def _build_fallback():
    fallback = logging.getLogger(FALLBACK_LOGGER_NAME)
    if not fallback.handlers:
        # 没有 handler 时不要向 stderr 抛 "No handlers could be found"；
        # 内核入口会按需配置 logging。
        fallback.addHandler(logging.NullHandler())
    fallback.setLevel(logging.INFO)
    return fallback


host = _resolve_host_logger()
logger = host if host is not None else _build_fallback()

#: 便于诊断"当前是否运行在 AstrBot 内"。
in_astrbot = host is not None
