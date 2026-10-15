"""Guardian 功能验证脚本：在真实 AstrBot 容器内带真实配置跑一轮巡检。

用法（宿主机）：
    docker exec -i astrbot-local python - < tests/func_patrol.py

只读性质：dry_run 保持 true，不执行任何真实动作；只写审计/状态文件。
"""

import asyncio
import io
import json
import os
import sys
import traceback

PLUGIN = "/AstrBot/data/plugins/astrbot_plugin_guardian"
ROOT = os.path.dirname(PLUGIN)
for p in (PLUGIN, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

PKG = "astrbot_plugin_guardian"

CONFIG_PATH = "/AstrBot/data/config/astrbot_plugin_guardian_config.json"
LEVELS = [
    {"id": "L0", "name": "常规", "after_hours": 0, "prompt": "", "action": "none"},
    {"id": "L1", "name": "I 级 · 轻提醒", "after_hours": 168,
     "prompt": "你已经连续在线工作 {uptime_days} 天了。", "action": "notify"},
    {"id": "L2", "name": "II 级 · 建议休息", "after_hours": 360,
     "prompt": "你已经连续在线 {uptime_days} 天没休息了。", "action": "notify"},
    {"id": "L3", "name": "III 级 · 准备睡眠", "after_hours": 600,
     "prompt": "你已连续在线 {uptime_days} 天。", "action": "arm_sleep"},
    {"id": "L4", "name": "IV 级 · 强制睡眠", "after_hours": 720,
     "prompt": "你已到达 30 天硬上限，必须休息。", "action": "force_sleep"},
]


def load_config() -> dict:
    try:
        return json.load(io.open(CONFIG_PATH, encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(f"读取配置失败({exc})，用内置默认")
        return {
            "dry_run": True,
            "containers": {"astrbot": "astrbot-local", "napcat": "napcat-local",
                           "managed": ["astrbot-local", "napcat-local"]},
            "safety": {"allowed_containers": ["astrbot-local", "napcat-local"]},
            "lifecycle": {"sleep_targets": ["napcat-local"],
                          "uptime_targets": ["napcat-local"], "levels": LEVELS},
        }


class FakeContext:
    """最小 Context 桩：Notifier 只在 umo_list 非空时才调 send_message。"""

    async def send_message(self, *a, **kw):
        raise RuntimeError("FakeContext.send_message 不应被调用")

    def get_llm_tool_manager(self):
        return None


async def main() -> int:
    runtime_mod = __import__(f"{PKG}.runtime", fromlist=["*"])
    GuardianRuntime = runtime_mod.GuardianRuntime

    rt = GuardianRuntime(FakeContext(), load_config())
    print("数据目录:", rt.data_dir)
    print("配置问题:", rt.config.issues or "(无)")

    try:
        await rt.start()
        print("runtime.start() OK")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2

    print("\n--- 触发一轮完整巡检 ---")
    try:
        out = await rt.engine.run_once(scope="all", trigger="manual-test")
    except Exception:  # noqa: BLE001
        print("!!! run_once 抛异常：")
        traceback.print_exc()
        await rt.stop()
        return 3

    print(f"skipped={out.get('skipped')} elapsed_ms={out.get('elapsed_ms')}")
    for r in out.get("results", []):
        print(f"  [{r.get('status','?'):8s}] {r.get('probe','?'):16s} "
              f"{r.get('severity','?'):8s} {str(r.get('message',''))[:90]}")
    inc = out.get("incidents", [])
    print(f"事件数: {len(inc)}")
    for i in inc:
        print(f"  - {i.get('kind','?')} {i.get('probe','')}/{i.get('target','')} "
              f"{i.get('status','')} {str(i.get('message',''))[:70]}")

    print("\n--- 生命周期 ---")
    print(await rt.lifecycle_text())

    print("\n--- 状态快照（节选） ---")
    snap = rt.state.snapshot() if hasattr(rt.state, "snapshot") else {}
    keys = ("mode", "profile", "night_until", "lifecycle.state", "last_event_age")
    for k in keys:
        print(f"  {k} = {snap.get(k)!r}")

    print("\n--- 审计尾部 5 条 ---")
    audit_path = os.path.join(rt.data_dir, "guardian", "audit.jsonl")
    if os.path.exists(audit_path):
        lines = io.open(audit_path, encoding="utf-8").read().strip().splitlines()
        for ln in lines[-5:]:
            try:
                d = json.loads(ln)
                print(f"  {d.get('kind','?')} target={d.get('target','')} "
                      f"ok={d.get('ok')} detail={str(d.get('detail',''))[:60]}")
            except Exception:  # noqa: BLE001
                print("  (坏行)", ln[:80])
    else:
        print("  (无审计文件)")

    await rt.stop()
    print("\n完成")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
