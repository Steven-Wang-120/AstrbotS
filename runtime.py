"""Guardian 守护系统 —— 运行时兼容壳（B16 §9 M1）。

对应文档：B16 §9（迁移计划 M1）、B16 §5.3（装配层如何拆）

**本文件自 M1 起不再承载任何业务逻辑**，只是历史导入路径的兼容层：

- 域 A 内核（决策 / 观察 / 记账，禁 `import astrbot`）→ `kernel_runtime.py`
- 域 B 适配层（AstrBot 面：数据目录 / 消息链 / 反向 WS 查询）→ `adapter_runtime.py`

`main.py` 仍然 `from .runtime import GuardianRuntime`，`GuardianRuntime` 现在是
`GuardianAdapter` 的别名，对外表面与拆分前完全一致。旧代码/脚本无需改动。

回滚方式（B16 §9 M1"回滚"列）：删除 `kernel_runtime.py` / `adapter_runtime.py`，
把本文件恢复为拆分前的实现即可——本壳不做任何隐藏的状态或副作用。
"""

from __future__ import annotations

from .adapter_runtime import (
    GuardianAdapter,
    GuardianRuntime,
    build_chain,
    resolve_data_dir,
)
from .kernel_runtime import (
    DEFAULT_DATA_DIR_ENV,
    PLUGIN_NAME,
    GuardianKernel,
    KernelRuntime,
    default_data_dir,
)

__all__ = [
    "DEFAULT_DATA_DIR_ENV",
    "PLUGIN_NAME",
    "GuardianAdapter",
    "GuardianKernel",
    "GuardianRuntime",
    "KernelRuntime",
    "build_chain",
    "default_data_dir",
    "resolve_data_dir",
]
