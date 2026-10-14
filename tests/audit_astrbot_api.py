"""Guardian 插件 —— 在真实 AstrBot 环境下的 API 兼容性审计。

用法（宿主机）：
    docker exec -i astrbot-local python - < tests/audit_astrbot_api.py

本脚本只读，不修改容器内任何文件。
"""

import importlib
import inspect
import os
import sys

PLUGIN = "/AstrBot/data/plugins/astrbot_plugin_guardian"
ROOT = os.path.dirname(PLUGIN)
for p in (PLUGIN, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

PKG = "astrbot_plugin_guardian"


def section(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# --------------------------------------------------------------------------- #
section("1. 逐模块导入（真实 AstrBot 4.26.x 环境）")
# --------------------------------------------------------------------------- #
mods = set()
for dirpath, dirnames, filenames in os.walk(PLUGIN):
    if "__pycache__" in dirpath:
        continue
    for fn in filenames:
        if not fn.endswith(".py"):
            continue
        rel = os.path.relpath(os.path.join(dirpath, fn), PLUGIN)
        parts = rel[:-3].split(os.sep)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            mods.add(".".join([PKG] + parts))

failed = []
for m in sorted(mods):
    try:
        importlib.import_module(m)
        print(f"  OK    {m}")
    except Exception as exc:  # noqa: BLE001
        failed.append((m, exc))
        print(f"  FAIL  {m}: {type(exc).__name__}: {exc}")

# --------------------------------------------------------------------------- #
section("2. filter 装饰器与真实 API 比对")
# --------------------------------------------------------------------------- #
try:
    from astrbot.api.event import filter as fmod  # 正确位置

    have = {n for n in dir(fmod) if not n.startswith("_")}
    used = {
        "command", "event_message_type", "llm_tool", "on_astrbot_loaded",
        "on_llm_request", "on_llm_tool_respond", "on_using_llm_tool",
    }
    for name in sorted(used):
        print(f"  {'OK   ' if name in have else 'MISS '} filter.{name}")
except Exception as exc:  # noqa: BLE001
    print(f"  filter 模块导入失败: {exc}")

# --------------------------------------------------------------------------- #
section("3. AstrMessageEvent 属性/方法核对")
# --------------------------------------------------------------------------- #
from astrbot.api.event import AstrMessageEvent

for attr in ("is_admin", "unified_msg_origin", "plain_result", "chain_result",
             "make_result", "get_message_str", "message_str", "role",
             "get_sender_id", "get_platform_name", "get_self_id", "send_message"):
    present = hasattr(AstrMessageEvent, attr)
    kind = ""
    if present:
        obj = inspect.getattr_static(AstrMessageEvent, attr)
        kind = type(obj).__name__
    print(f"  {'OK   ' if present else 'MISS '} AstrMessageEvent.{attr} {kind}")

# --------------------------------------------------------------------------- #
section("4. Star / StarTools / Context 能力核对")
# --------------------------------------------------------------------------- #
from astrbot.api.star import Star, StarTools
from astrbot.api.star import Context

try:
    sig = inspect.signature(Star.__init__)
    print(f"  Star.__init__{str(sig)[:100]}")
except Exception as exc:  # noqa: BLE001
    print(f"  Star.__init__ 读取失败: {exc}")

for cls, names in (
    (Star, ("terminate", "get_kv_data", "put_kv_data", "delete_kv_data")),
    (StarTools, ("get_data_dir", "register_llm_tool", "activate_llm_tool")),
    (Context, ("send_message", "add_llm_tools", "get_llm_tool_manager",
               "get_using_provider", "llm_generate")),
):
    have = {n for n in dir(cls) if not n.startswith("_")}
    for n in names:
        print(f"  {'OK   ' if n in have else 'MISS '} {cls.__name__}.{n}")

# --------------------------------------------------------------------------- #
section("5. 附带导入核对")
# --------------------------------------------------------------------------- #
for modpath, name in (
    ("astrbot.api.message_components", "Plain"),
    ("astrbot.core.agent.message", "TextPart"),
    ("astrbot.core.star.register", "register_star"),
):
    try:
        mod = importlib.import_module(modpath)
        ok = hasattr(mod, name)
        print(f"  {'OK   ' if ok else 'MISS '} {modpath}.{name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {modpath}: {type(exc).__name__}: {exc}")

# --------------------------------------------------------------------------- #
section("6. Star 子类的 handler 收集检查")
# --------------------------------------------------------------------------- #
try:
    importlib.reload(importlib.import_module(f"{PKG}.main"))
    from astrbot.api.event import filter as fm

    # 用与 star_manager 相同的方式取 handlers
    try:
        from astrbot.core.star.star_map import StarMap  # noqa: F401
    except Exception:  # noqa: BLE001
        pass

    print("  main 模块已导入，尝试列出被装饰的 handler：")
    for attr in dir(importlib.import_module(f"{PKG}.main")):
        obj = getattr(importlib.import_module(f"{PKG}.main"), attr)
        if not (inspect.isfunction(obj) or inspect.ismethod(obj)):
            continue
        decs = []
        for d in ("command", "llm_tool", "on_astrbot_loaded", "on_llm_request",
                  "on_using_llm_tool", "on_llm_tool_respond",
                  "event_message_type"):
            if hasattr(obj, "__astrbot_decorator__"):
                decs.append(f"deco={getattr(obj, '__astrbot_decorator__')!r}")
        print(f"    {attr} {' '.join(decs)}")
except Exception as exc:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    print(f"  handler 检查失败: {exc}")

# --------------------------------------------------------------------------- #
section("汇总")
# --------------------------------------------------------------------------- #
print(f"  模块导入失败数: {len(failed)}")
for m, exc in failed:
    print(f"    - {m}: {type(exc).__name__}: {exc}")
