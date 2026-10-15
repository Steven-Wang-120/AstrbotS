"""Guardian 守护系统 —— 内核解耦自检（B16 独立内核服务的前提）。

对应文档：B16 §"内核/适配层边界"

**为什么需要这个测试**：独立内核服务必须在 AstrBot 进程之外运行，否则它在睡眠/
重启本体容器时会把自己一起杀死。只要内核侧任何一个模块直接 `import astrbot`，
独立进程就会在导入期崩溃。所以这不是风格问题，是**可运行性**问题。

本测试做两件互相独立的事，任一件失败都说明内核还不能独立启动：

1. **静态不变量**：`core/ probes/ utils/ actions/ notify/ modes/ tools/` 下除
   `core/log.py`（唯一的日志门面）外，任何文件都不得出现 `import astrbot`。
2. **动态可导入**：把 `astrbot` 从 `sys.modules` 移除并**主动阻断**其导入，
   然后逐个导入上述包的全部模块，必须全部成功。

直接运行：`python tests/test_kernel_decoupling.py`
"""

from __future__ import annotations

import importlib
import importlib.abc
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from pathlib import Path  # noqa: E402

PLUGIN = Path(SRC) / "astrbot_plugin_guardian"

#: 内核侧包：这些包必须能在没有 astrbot 的进程里导入。
KERNEL_PACKAGES = ("core", "probes", "utils", "actions", "notify", "modes", "tools",
                   "ipc", "kernel")

#: 内核侧**顶层**模块（不属于上面的包，但同样必须能脱离 astrbot 导入）。
#: M1 之后内核装配被拆到 kernel_runtime.py，它必须保持零 astrbot 依赖。
KERNEL_TOP_LEVEL_MODULES = ("kernel_runtime.py",)

#: 唯一允许提及 astrbot 的内核侧文件（日志门面，内部 try/except 转发）。
ALLOWED_ASTRBOT_REFERENCES = {"core/log.py"}


# --------------------------------------------------------------------------- #
# 1. 静态不变量
# --------------------------------------------------------------------------- #
def _kernel_source_files():
    for package in KERNEL_PACKAGES:
        directory = PLUGIN / package
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            yield path, str(path.relative_to(PLUGIN)).replace("\\", "/")
    for name in KERNEL_TOP_LEVEL_MODULES:
        path = PLUGIN / name
        if path.is_file():
            yield path, name


def test_no_kernel_module_imports_astrbot():
    offenders: list[str] = []
    for path, relative in _kernel_source_files():
        if relative in ALLOWED_ASTRBOT_REFERENCES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if (stripped.startswith("from astrbot")
                    or stripped.startswith("import astrbot")):
                offenders.append(f"{relative}:{number}: {stripped}")
    assert not offenders, (
        "内核侧模块不得直接依赖 AstrBot（内核必须能脱离 AstrBot 独立运行）：\n  "
        + "\n  ".join(offenders)
    )


def test_only_the_adapter_touches_astrbot():
    """把"允许提及 astrbot"的范围钉死，防止后续悄悄扩大耦合面。

    自 M1 起 `runtime.py` 只是兼容壳、不再直接 import astrbot，故已从白名单移除；
    真正接触 AstrBot 的只剩 `main.py`（插件入口）与 `adapter_runtime.py`（域 B）。
    """
    allowed = {"main.py", "adapter_runtime.py", "core/log.py", "__init__.py"}
    offenders: list[str] = []
    for path in sorted(PLUGIN.rglob("*.py")):
        relative = str(path.relative_to(PLUGIN)).replace("\\", "/")
        if relative in allowed:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if (stripped.startswith("from astrbot")
                    or stripped.startswith("import astrbot")):
                offenders.append(f"{relative}:{number}: {stripped}")
    assert not offenders, (
        "除 AstrBot 适配层（main.py / adapter_runtime.py）与日志门面（core/log.py）外，"
        "不得出现 astrbot 导入：\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# 2. 动态可导入（真正阻断 astrbot）
# --------------------------------------------------------------------------- #
class _AstrbotBlocker(importlib.abc.MetaPathFinder):
    """让任何 `import astrbot` 直接失败，模拟"内核进程里没有 AstrBot"。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "astrbot" or fullname.startswith("astrbot."):
            raise ImportError(f"astrbot is blocked by test_kernel_decoupling: {fullname}")
        return None


def _kernel_module_names() -> list[str]:
    names: list[str] = []
    for package in KERNEL_PACKAGES:
        directory = PLUGIN / package
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            parts = list(path.relative_to(PLUGIN).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            names.append("astrbot_plugin_guardian." + ".".join(parts))
    for name in KERNEL_TOP_LEVEL_MODULES:
        if (PLUGIN / name).is_file():
            names.append("astrbot_plugin_guardian." + name[:-3])
    return names


def test_kernel_packages_import_without_astrbot():
    saved_modules = {k: v for k, v in sys.modules.items()
                     if k == "astrbot" or k.startswith(("astrbot.", "astrbot_plugin_guardian"))}
    blocker = _AstrbotBlocker()
    for name in list(saved_modules):
        sys.modules.pop(name, None)
    sys.meta_path.insert(0, blocker)
    try:
        names = _kernel_module_names()
        assert names, "没有发现任何内核侧模块，路径可能不对"
        failures: list[str] = []
        for name in names:
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        assert not failures, (
            "以下内核侧模块在 astrbot 缺席时无法导入（内核将无法独立启动）：\n  "
            + "\n  ".join(failures)
        )

        # 日志门面必须退化到标准库，而不是崩掉
        log_module = importlib.import_module("astrbot_plugin_guardian.core.log")
        assert log_module.in_astrbot is False, "astrbot 被阻断时 in_astrbot 应为 False"
        for method in ("debug", "info", "warning", "error", "critical", "exception"):
            assert callable(getattr(log_module.logger, method, None)), method
    finally:
        sys.meta_path.remove(blocker)
        for name in list(sys.modules):
            if name == "astrbot" or name.startswith(("astrbot.", "astrbot_plugin_guardian")):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


# --------------------------------------------------------------------------- #
# 3. M1 验收（B16 §9）：内核能在**没有 astrbot 的解释器**里构造并跑完一轮 patrol
# --------------------------------------------------------------------------- #
def test_kernel_constructs_and_runs_patrol_without_astrbot():
    """这是 M1 的验收判据：内核（域 A）不依赖 AstrBot 也能装配并巡检。

    做法：阻断 astrbot 导入，构造 `GuardianKernel`（注入假 docker / 假通知发送器），
    然后真正跑一轮 `run_once`，要求不抛异常、且探针确实产出了结果。
    """
    import asyncio
    import tempfile

    saved_modules = {k: v for k, v in sys.modules.items()
                     if k == "astrbot" or k.startswith(("astrbot.", "astrbot_plugin_guardian"))}
    blocker = _AstrbotBlocker()
    for name in list(saved_modules):
        sys.modules.pop(name, None)
    sys.meta_path.insert(0, blocker)
    try:
        kernel_mod = importlib.import_module("astrbot_plugin_guardian.kernel_runtime")
        assert kernel_mod.__dict__.get("logger") is not None

        class FakeDocker:
            """只读替身：docker daemon 正常、两个容器 running、内存稳定。"""

            async def negotiate(self):
                return "v1.44"

            async def ping(self):
                return True

            async def info(self):
                return {"Containers": 2, "ContainersRunning": 2}

            async def inspect(self, name):
                return {"State": {"Running": True, "RestartCount": 0, "OOMKilled": False,
                                  "StartedAt": "2026-10-14T10:00:00.000000000Z",
                                  "Status": "running"}}

            async def stats(self, name):
                return {"usage": 300 * 1024 * 1024, "limit": 4 * 1024 ** 3,
                        "stats": {"inactive_file": 10 * 1024 * 1024}}

            async def close(self):
                return None

        sent: list[tuple[str, str]] = []

        async def sender(umo, text):
            sent.append((umo, text))

        with tempfile.TemporaryDirectory() as tmp:
            kernel = kernel_mod.GuardianKernel(
                {"containers": {"managed": ["astrbot", "napcat"]},
                 "dry_run": True,
                 "notify": {"umo_list": []}},
                data_dir=tmp, sender=sender)
            kernel.deps["docker"] = FakeDocker()
            kernel.engine.deps["docker"] = kernel.deps["docker"]

            asyncio.run(kernel.engine.start())
            result = asyncio.run(kernel.run_check("all"))

            assert result.get("skipped") is False, result
            assert result["results"], "内核在无 astrbot 环境下没有跑出任何探针结果"
            assert any(r["probe"] == "uptime" for r in result["results"]), \
                [r["probe"] for r in result["results"]]
            # 内核数据目录必须落在自己指定的位置
            assert (Path(tmp) / "guardian" / "state.json").exists(), \
                list(Path(tmp).rglob("*"))
    finally:
        sys.meta_path.remove(blocker)
        for name in list(sys.modules):
            if name == "astrbot" or name.startswith(("astrbot.", "astrbot_plugin_guardian")):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def test_runtime_shell_stays_astrbot_free():
    """兼容壳 runtime.py 不得自己 import astrbot（只转发 adapter_runtime）。"""
    source = (PLUGIN / "runtime.py").read_text(encoding="utf-8")
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        assert not (stripped.startswith("from astrbot")
                    or stripped.startswith("import astrbot")), f"runtime.py:{number}"


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
