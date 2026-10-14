"""在真实 astrbot-local 容器内实测 /guardian 子命令（C16）。

覆盖：
  - F2：跨版本取消息文本（走 get_message_str() 方法而非 message_str 属性）
  - /guardian 命令面回归（status/help/level/incidents/jobs/safety/diagnose/别名/未知子命令）

运行：docker cp 进容器后 `python /tmp/cmd_probe.py`
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/AstrBot")
os.chdir("/AstrBot")

from data.plugins.astrbot_plugin_guardian import main as M  # noqa: E402


class FakeEvent:
    """最小事件替身：只提供 _event_text / is_admin / plain_result 所需接口。"""

    def __init__(self, text, admin=True):
        self._t = text
        self._admin = admin

    def get_message_str(self):  # AstrBot 4.x 的方法（4.26.7 实测存在）
        return self._t

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return text


class FakeContext:
    def get_config(self):
        return None

    async def send_message(self, session, chain):
        return None


CASES = [
    ("guardian", ["status"]),
    ("guardian help", ["help"]),
    ("guardian level", ["level"]),
    ("guardian incidents", ["incidents"]),
    ("guardian jobs", ["jobs"]),
    ("guardian safety", ["safety"]),
    ("guardian diagnose", ["diagnose"]),
    ("守护", ["level"]),
    ("guardian sleep 0 手动验证", ["sleep", "0", "手动验证"]),  # 干跑模式：不应真重启
    ("guardian wat", ["wat"]),
]


async def main():
    cfg_path = "/AstrBot/data/config/astrbot_plugin_guardian_config.json"
    with open(cfg_path, "rb") as f:
        data = json.loads(f.read().decode("utf-8-sig"))

    plugin = M.GuardianPlugin(FakeContext(), data)

    passed = failed = 0
    for text, args in CASES:
        ev = FakeEvent(text)
        try:
            out = await plugin._dispatch_command(ev, args)
            snippet = out.replace("\n", " | ")[:160]
            print(f"  PASS  {text!r:24} -> {snippet}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {text!r:24} -> {type(exc).__name__}: {exc}")
            failed += 1

    # F2 专项：确认取文本走 get_message_str() 方法
    ev = FakeEvent("guardian status")
    got = M._event_text(ev)
    assert got == "guardian status", got
    print("  PASS  _event_text() 走 get_message_str() 方法")
    passed += 1

    # F2 兜底：老版本只有 message_str 属性时也能取到
    class LegacyEvent:
        message_str = "guardian status"

    assert M._event_text(LegacyEvent()) == "guardian status"
    print("  PASS  _event_text() 兼容 message_str 属性（旧版回退）")
    passed += 1

    print(f"\nRESULT: PASS={passed} FAIL={failed}")
    return 0 if failed == 0 else 1


sys.exit(asyncio.run(main()))
