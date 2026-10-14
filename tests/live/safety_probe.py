"""在真实容器内验证 F5（逻辑目标授权）与 F7b（kind 感知目标名校验），使用线上真实配置。

覆盖：
  - qq_relogin(target="qq") 不再被 not_allowlisted 误杀
  - ros2_restart(target="/node_x") 不再被 bad_target 误杀
  - 普通容器动作仍走白名单（astrbot-local 放行，陌生容器拒绝）
  - 真掉线证据下 qq_relogin 的完整授权链路（干跑模式只记录不执行）

运行：docker cp 进容器后 `python /tmp/safety_probe.py`
"""
import json
import sys

sys.path.insert(0, "/AstrBot")

from data.plugins.astrbot_plugin_guardian.core.audit import AuditLog  # noqa: E402
from data.plugins.astrbot_plugin_guardian.core.config import build_config  # noqa: E402
from data.plugins.astrbot_plugin_guardian.core.models import ActionSpec  # noqa: E402
from data.plugins.astrbot_plugin_guardian.core.safety import SafetyGuard  # noqa: E402
from data.plugins.astrbot_plugin_guardian.core.state import StateStore  # noqa: E402

DATA_DIR = "/AstrBot/data/plugin_data/astrbot_plugin_guardian/guardian"


def build_guard():
    with open("/AstrBot/data/config/astrbot_plugin_guardian_config.json", "rb") as f:
        raw = json.loads(f.read().decode("utf-8-sig"))
    cfg = build_config(raw, data_dir=DATA_DIR)
    state = StateStore("/tmp/guardian_safety_probe_state.json")
    audit = AuditLog(DATA_DIR)
    return SafetyGuard(cfg, state, audit), cfg


def main() -> int:
    guard, cfg = build_guard()
    print(f"dry_run={cfg.dry_run}  action_allowlist={sorted(cfg.action_allowlist)}")
    print(f"qq.relogin_method={getattr(cfg.qq, 'relogin_method', '?')}")

    passed = failed = 0

    def check(label, spec, *, expect_allow, expect_code=None):
        nonlocal passed, failed
        dec = guard.authorize(spec)
        ok = (dec.allowed is expect_allow)
        if expect_code is not None and dec.code != expect_code:
            ok = False
        flag = "PASS" if ok else "FAIL"
        print(f"  {flag}  {label:38} allowed={dec.allowed!s:5} code={dec.code:20} {dec.reason}")
        if ok:
            passed += 1
        else:
            failed += 1

    # F5：逻辑目标不再被容器白名单误杀（干跑模式 -> 放行但不执行）
    check("qq_relogin / qq（逻辑目标）",
          ActionSpec(kind="qq_relogin", target="qq", actor="system",
                     trigger="interval", reason="probe"),
          expect_allow=True)

    # F7b：ROS2 风格节点名能通过目标名校验（不再 bad_target）；
    # 但未配置恢复命令时必须被拒 —— 这是正确的“缺配置即拒绝”，不是 bad_target。
    check("ros2_restart / /node_x（未配命令 -> 拒）",
          ActionSpec(kind="ros2_restart", target="/node_x", actor="system",
                     trigger="interval", reason="probe"),
          expect_allow=False, expect_code="ros2_no_command")
    # ROS2 名若非 "/" 开头（当成容器名处理）才应判 bad_target
    check("ros2_restart / 非法名 ..",
          ActionSpec(kind="ros2_restart", target="..", actor="system",
                     trigger="interval", reason="probe"),
          expect_allow=False, expect_code="bad_target")

    # 普通容器动作仍严格走白名单
    # 白名单内且非受保护容器 -> 干跑放行
    if "napcat-local" in cfg.action_allowlist:
        check("restart_container / napcat-local（白名单内）",
              ActionSpec(kind="restart_container", target="napcat-local",
                         actor="system", trigger="interval", reason="probe"),
              expect_allow=True)
    # 白名单内但属于受保护容器（自身）-> 必须 protected
    if "astrbot-local" in cfg.action_allowlist:
        check("restart_container / astrbot-local（受保护）",
              ActionSpec(kind="restart_container", target="astrbot-local",
                         actor="system", trigger="interval", reason="probe"),
              expect_allow=False, expect_code="protected")
    check("restart_container / unknown-box（白名单外）",
          ActionSpec(kind="restart_container", target="unknown-box",
                     actor="system", trigger="interval", reason="probe"),
          expect_allow=False, expect_code="not_allowlisted")

    # F7b：容器名仍拒绝非法字符
    check("restart_container / 非法名 ..",
          ActionSpec(kind="restart_container", target="..", actor="system",
                     trigger="interval", reason="probe"),
          expect_allow=False, expect_code="bad_target")

    # F5 反向：relogin_method=disabled 时必须拒绝
    cfg.qq.relogin_method = "disabled"
    check("qq_relogin / qq（relogin_method=disabled）",
          ActionSpec(kind="qq_relogin", target="qq", actor="system",
                     trigger="interval", reason="probe"),
          expect_allow=False, expect_code="relogin_disabled")

    print(f"\nRESULT: PASS={passed} FAIL={failed}")
    return 0 if failed == 0 else 1


sys.exit(main())
