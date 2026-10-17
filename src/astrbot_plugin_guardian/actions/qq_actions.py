"""Guardian 守护系统 —— QQ 登录恢复阶梯（B17 §7）。

v0.5.0 的 `qq_relogin` 只有"重启容器"一步：能不能回来完全取决于 NapCat 镜像
entrypoint 里的 `qq -q $ACCOUNT`。会话过期 / 被踢 / 要验证码时，重启多少次都
回不来，只能人工扫码。v0.6.0 把它升级成四段式阶梯：

    [1] 前置校验（dry-run / disabled / 凭据缺失 / 密码失败静默期）
    [2] 有条件重启：容器启动距今 ≥ restart_grace_sec 才重启
    [3] 等待就绪：轮询 CheckLoginStatus，直到 isLogin 或 qrcodeurl 非空
    [4] 账密登录：PasswordLogin → 四类结果分支
    [5] 复核 + 闭环：GetQQLoginInfo.online → SetQuickLoginQQ 写回 autoLoginAccount
    [6] 失败出口：按"QQ 是否在线"选择通知通道

**重启仍是第一段，不弱化**（G2）。三条硬不变量贯穿全文：

- I3 凭据不外流：密码只从 `config.qq.password` 现读，永不进 `spec.params`、
  `Incident`、`ProbeResult.raw`、`audit.detail`。
- I4 失败不静默：三类失败各有独立审计 `kind`，且必须产出可见提示。
- I6 登录失败不得追加重启：本文件里只有**一处**容器重启调用点，位于 [2]，
  且由"是否刚重启过"单向决定；[4] 之后的分支不得再碰容器。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.log import logger

from ..core.models import ActionResult, ActionSpec, Severity
from ..utils.dockerclient import DockerError, DockerUnavailable
from ..utils.napcat import NapCatAuthError, NapCatError
from .base import Action, register_action

#: `kind` → 审计 kind（B17 §7.3）。三类失败必须各自可见，不能合并成一条
#: "登录失败"，否则运维分不清"密码错"和"要验证码"。
FAILURE_AUDIT = {
    "captcha": "qq.login_captcha",
    "new_device": "qq.login_new_device",
    "failed": "qq.login_failed",
}

#: 这三类失败会写静默期：反复拿错密码去试会触发 QQ 风控甚至锁号。
HOLD_KINDS = {"captcha", "new_device", "failed"}

_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|[+-]\d{2}:?\d{2})?$")


def parse_rfc3339(value: str) -> float:
    """解析 Docker 的 `StartedAt`（RFC3339Nano，纳秒精度）。

    `datetime.fromisoformat` 在 Python 3.10 及以前只吃 3/6 位小数，而 Docker
    给的是 9 位（如 `2026-10-17T06:15:06.123456789Z`）。这里把小数位截断到
    微秒再解析；**解析失败一律抛 ValueError**，由调用方按"保守选择重启"处理。
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("StartedAt 为空")
    m = _RFC3339.match(text)
    if not m:
        raise ValueError(f"StartedAt 不是 RFC3339: {text[:40]}")
    year, month, day, hour, minute, second, frac, tz = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    if tz in (None, "Z"):
        tzinfo = timezone.utc
    else:
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        tzinfo = timezone(sign * timedelta(
            hours=int(digits[:2]), minutes=int(digits[2:4])))
    dt = datetime(int(year), int(month), int(day), int(hour), int(minute),
                  int(second), micro, tzinfo=tzinfo)
    return dt.timestamp()


@register_action("qq_relogin")
class QqReloginAction(Action):
    kind = "qq_relogin"
    #: `_password_login` 的分类结果。放在类属性上是为了让 `execute` 的失败分支
    #: 永远读得到确定值，不依赖"是否已经走过账密阶段"。
    _last_fail_kind: str = "failed"
    _last_fail_message: str = ""
    _last_fail_url: str = ""

    # ================================================================== #
    # 主流程
    # ================================================================== #
    async def execute(self, spec: ActionSpec) -> ActionResult:
        started = time.time()
        cfg = self.config.qq
        method = str(cfg.relogin_method or "restart_container")

        # ---- [1] 前置校验 --------------------------------------------- #
        if spec.dry_run:
            # B12 / B17 §12.3：不重启、不换 Credential、不发密码，只预演。
            return self._result(
                True, f"[dry-run] 将按 {method} 恢复 QQ 登录（不重启、不换 Credential、不发密码）",
                started)
        if method == "disabled":
            return self._result(False, "重登策略为 disabled，仅告警（请人工处理）", started)

        steps: list[str] = []
        napcat_name = str(self.config.containers.napcat)

        # ---- [2] 有条件重启（全文件唯一的容器重启点，I6） --------------- #
        if method == "restart_container":
            skip, note = await self._should_skip_restart(napcat_name)
            steps.append(note)
            if not skip:
                try:
                    await self.docker.restart(napcat_name, timeout=15)
                except (DockerUnavailable, DockerError) as exc:
                    self.audit.append(kind="qq.restart_failed", target="qq",
                                      detail=f"重启 {napcat_name} 失败: {exc}", ok=False)
                    return self._result(False, "；".join(steps + [f"重启 {napcat_name} 失败: {exc}"]),
                                        started)

        # ---- [3] 等待就绪 --------------------------------------------- #
        ready, note = await self._wait_login_ready()
        steps.append(note)

        # 就绪窗口超时 ≠ 恢复失败。重启后 NapCat 起进程 + QQ 协议登录可能比
        # `login_ready_timeout` 稍慢，此时 CheckLoginStatus 仍是 isLogin=false，
        # 但 QQ 其实马上就回来了。**先复核再下结论**，否则会把一次成功的恢复
        # 报成失败，并让上层白跑一遍"未到登录页"的告警（K-01 实机发现）。
        online, vnote = await self._verify_online(attempts=1)
        if online:
            # 会话仍有效：快速登录已把账号带回来，此时**不得**再发密码（A1 / F6）。
            steps.append(f"复核在线：{vnote}")
            await self._finalize_quick_login(steps)
            await self._on_success()
            await self._notify_ok(steps)
            return self._result(True, "；".join(steps), started)
        steps.append(f"复核未在线：{vnote}")

        # ---- [4] 账密登录 --------------------------------------------- #
        hold_left = self._hold_remaining()
        if hold_left > 0:
            steps.append(f"账密登录处于静默期（剩余 {int(hold_left)}s），本轮跳过")
            await self._handle_failure(steps, kind="hold", message="静默期内不再尝试账密")
            return self._result(False, "；".join(steps), started)

        if not ready:
            await self._handle_failure(
                steps, kind="not_ready",
                message=f"QQ 客户端未到登录页（等待 {cfg.login_ready_timeout}s 超时）")
            return self._result(False, "；".join(steps), started)

        if not (cfg.account and cfg.password):
            await self._handle_failure(
                steps, kind="no_credential",
                message="仅重启，未配凭据（qq.account / qq.password 为空）")
            return self._result(False, "；".join(steps), started)

        ok, note = await self._password_login()
        steps.append(note)
        if not ok:
            # 失败分支绝不追加任何容器动作（I6）。
            await self._handle_failure(steps, kind=self._last_fail_kind,
                                       message=self._last_fail_message,
                                       url=self._last_fail_url)
            return self._result(False, "；".join(steps), started)

        # ---- [5] 复核 + 闭环 ------------------------------------------ #
        online, vnote = await self._verify_online()
        if not online:
            # R2：PasswordLogin 说成功但协议端没上线，不算成功。
            steps.append(f"账密登录返回成功，但复核未通过：{vnote}")
            await self._handle_failure(steps, kind="failed",
                                       message=f"账密登录后复核未通过：{vnote}")
            return self._result(False, "；".join(steps), started)

        steps.append(f"复核在线：{vnote}")
        await self._finalize_quick_login(steps)
        await self._on_success()
        await self._notify_ok(steps)
        return self._result(True, "；".join(steps), started)

    # ================================================================== #
    # [2] 有条件重启判据（B17 §4.2）
    # ================================================================== #
    async def _should_skip_restart(self, name: str) -> tuple[bool, str]:
        """容器启动距今 < `restart_grace_sec` 时跳过重启。

        为什么需要它：维护重启、睡眠唤醒、宿主机看门狗都会重启 napcat。若这些
        重启后 QQ 没起来，探针要 2 轮（默认 40 分钟）才判 CRIT，此时再重启一次
        就是纯粹的多余中断。**跳过重启不等于跳过动作**——[3][4][5] 照常执行，
        那正是补登录的地方。

        判据直接读 Docker 事实，不复用任何新状态：任何缓存都会与"容器刚被别人
        重启过"这个外部事件失同步。解析失败按"需要重启"处理（保守）。
        """
        grace = float(self.config.qq.restart_grace_sec or 0)
        if grace <= 0:
            return False, "restart_grace_sec=0，本轮强制重启"
        try:
            info = await self.docker.inspect(name)
        except (DockerUnavailable, DockerError) as exc:
            return False, f"读取 {name} 启动时间失败({exc})，保守选择重启"
        state = info.get("State") if isinstance(info, dict) else None
        started_at = str((state or {}).get("StartedAt") or "")
        try:
            age = time.time() - parse_rfc3339(started_at)
        except (ValueError, TypeError, OverflowError):
            return False, f"{name} 的 StartedAt 无法解析，保守选择重启"
        if age < grace:
            return True, (f"{name} 启动仅 {max(0, int(age))}s < 宽限 {int(grace)}s，"
                          "跳过重启（仍执行登录恢复）")
        return False, f"{name} 已运行 {int(age)}s ≥ 宽限 {int(grace)}s，执行重启"

    # ================================================================== #
    # [3] 就绪等待
    # ================================================================== #
    async def _wait_login_ready(self) -> tuple[bool, str]:
        """轮询 `CheckLoginStatus`，直到已登录或二维码可用。

        `isLogin` 先判：账号已在线时 `qrcodeurl` 恒为空，只判二维码会白等一整个
        超时窗口。`qrcodeurl` 非空 = QQ 客户端已到登录页，此时账密接口才可用。
        """
        cfg = self.config.qq
        timeout = float(cfg.login_ready_timeout or 90)
        interval = float(cfg.login_retry_interval or 3)
        deadline = time.time() + timeout
        last = "无响应"
        while True:
            try:
                status = await self.napcat.check_login_status()
            except NapCatAuthError as exc:
                return False, f"NapCat WebUI 鉴权失败：{exc}"
            except NapCatError as exc:
                last = str(exc)
                logger.debug(f"[guardian] 登录状态查询失败: {exc}")
            else:
                if status.get("isLogin"):
                    return True, "已登录"
                if status.get("qrcodeurl"):
                    return True, "QQ 客户端已到登录页"
                last = status.get("loginError") or "未到登录页"
            remaining = deadline - time.time()
            if remaining <= 0:
                return False, f"等待 {int(timeout)}s 仍未到登录页（{last}）"
            await asyncio.sleep(min(interval, max(0.05, remaining)))

    # ================================================================== #
    # [4] 账密登录
    # ================================================================== #
    async def _password_login(self) -> tuple[bool, str]:
        """调 `PasswordLogin` 并按四类结果分类。

        ⚠️ 密码在这里现读、现算 md5、当场用完，**不落地、不进 detail、不进日志**。
        日志里只有 uin 与 `credential=present/absent`（B17 §12.1）。
        """
        cfg = self.config.qq
        self._last_fail_kind = "failed"
        self._last_fail_message = ""
        self._last_fail_url = ""
        password_md5 = (cfg.password if cfg.password_is_md5
                        else hashlib.md5(str(cfg.password).encode("utf-8")).hexdigest())
        logger.info(f"[guardian] 账密登录 uin={cfg.account} credential=present")
        try:
            res = await self.napcat.password_login(str(cfg.account), password_md5)
        except NapCatAuthError as exc:
            self._last_fail_kind = "auth"
            self._last_fail_message = f"WebUI token 无效：{exc}"
            return False, f"账密登录中止：WebUI token 无效（{exc}）"
        except NapCatError as exc:
            self._last_fail_kind = "failed"
            self._last_fail_message = str(exc)
            return False, f"账密登录请求失败：{exc}"
        finally:
            password_md5 = ""  # noqa: F841 —— 主动丢弃局部引用

        kind = str(res.get("kind") or "failed")
        message = str(res.get("message") or "")
        url = str(res.get("url") or "")
        if res.get("ok"):
            return True, "账密登录成功"

        self._last_fail_kind = kind
        self._last_fail_message = message
        self._last_fail_url = url
        if kind == "captcha":
            return False, f"账密登录需要验证码：{message}（人工接管链接已记录）"
        if kind == "new_device":
            return False, f"账密登录需要新设备验证：{message}（人工接管链接已记录）"
        return False, f"账密登录失败：{message or '密码或账号不正确'}"

    # ================================================================== #
    # [5] 复核 + 闭环
    # ================================================================== #
    async def _verify_online(self, attempts: int = 3, wait: float = 3.0) -> tuple[bool, str]:
        """复核 `GetQQLoginInfo.online`。

        旧实现只判 OneBot 的 `user_id` 非空。协议端"已加载账号"与"真的在线"是
        两件事，所以这里以 WebUI 的 `online` 为准；只有 WebUI 完全不可用时才回落
        OneBot 证据（并明确标注证据来源，别让人误以为是同一强度）。
        """
        last = "无响应"
        for i in range(max(1, attempts)):
            if i:
                await asyncio.sleep(wait)
            try:
                info = await self.napcat.get_qq_login_info()
            except NapCatAuthError as exc:
                last = f"WebUI 鉴权失败：{exc}"
            except NapCatError as exc:
                last = str(exc)
                fallback = await self._verify_by_onebot()
                if fallback:
                    return True, f"{fallback}（WebUI 不可用，降级为 OneBot 证据）"
            else:
                if info.get("online"):
                    nick = info.get("nick") or "-"
                    return True, f"uin={info.get('uin') or '-'} nick={nick}"
                last = "GetQQLoginInfo.online=false"
                logger.debug(f"[guardian] 复核第 {i + 1} 次：账号尚未在线")
        return False, last

    async def _verify_by_onebot(self) -> str:
        try:
            data = (await self.napcat.get_login_info()).get("data") or {}
        except NapCatError:
            return ""
        uid = str(data.get("user_id") or "").strip()
        if uid and uid not in ("0", "None"):
            return f"user_id={uid}"
        return ""

    async def _finalize_quick_login(self, steps: list[str]) -> None:
        """写回 `autoLoginAccount`，让下次容器重启能靠 `-q` 自己回来。

        失败**不致命**：账号此刻已经在线，写回失败只是丢了"下次免登录"的便利，
        不该把一次成功的恢复判成失败（B17 T-08）。
        """
        uin = str(self.config.qq.account or "").strip()
        if not uin:
            return
        try:
            await self.napcat.set_quick_login_qq(uin)
        except NapCatError as exc:
            steps.append(f"写回失败（autoLoginAccount）：{exc}")
            self.audit.append(kind="qq.quick_login_writeback_failed", target="qq",
                              detail=f"uin={uin} 写回失败: {exc}", ok=False)
            logger.warning(f"[guardian] 写回 autoLoginAccount 失败: {exc}")
            return
        steps.append("已写回 autoLoginAccount")

    # ================================================================== #
    # [6] 失败出口（B17 §4.3 通道选择矩阵）
    # ================================================================== #
    async def _handle_failure(self, steps: list[str], *, kind: str,
                              message: str, url: str = "") -> None:
        """失败必须"分级 + 可见 + 不循环"。

        通道选择是关键：QQ 掉线时通过 `context.send_message` 推 QQ **必失败**，
        而且旧版 `notifier` 会因此把管理员 UMO 永久拉黑（缺陷 P4），于是 QQ 恢复
        后连"已恢复"都收不到。所以离线时改走 WebUI 页面 + 落盘 + 审计。
        """
        self._record_failure(kind, message, url)
        # 静默期必须**立即**落盘。`StateStore.flush()` 平时是按巡检周期防抖的，
        # 而账密失败是"最不该丢"的状态：进程若在下一个巡检周期前被强杀，
        # 内存里的 hold 就没了，重启后会立刻拿密码再撞一次风控（可能锁号）。
        await self.state.flush(force=True)
        text = self._failure_text(kind, message, url)
        steps.append(text)
        if await self._is_online():
            await self._push(text)
            return
        # 离线：不发 QQ（发必失败且会误拉黑），改落盘 + 审计。
        self.audit.append(kind="notify.suppressed_offline", target="qq",
                          detail=f"QQ 离线，登录失败通知改走 WebUI：{message[:120]}")
        self._write_error_file(text)
        logger.warning(f"[guardian] {text}（QQ 离线，通知已抑制；详见 WebUI 登录状态页）")

    def _record_failure(self, kind: str, message: str, url: str = "") -> None:
        audit_kind = FAILURE_AUDIT.get(kind, f"qq.login_{kind}")
        if kind == "hold":
            audit_kind = "qq.login_hold"
        detail = message if not url else f"{message}（人工接管：{url}）"
        self.audit.append(kind=audit_kind, target="qq", detail=detail, ok=False)
        self.state.set("qq.last_failure", {
            "kind": kind, "message": message, "url": url, "ts": time.time(),
        })
        if kind in HOLD_KINDS:
            hold = float(self.config.qq.password_fail_hold_sec or 0)
            self.state.set("qq.auth_hold_until", time.time() + hold)

    @staticmethod
    def _failure_text(kind: str, message: str, url: str) -> str:
        head = {
            "captcha": "需要验证码，已停止重试",
            "new_device": "需要新设备验证，已停止重试",
            "failed": "密码或账号不正确，已停止重试",
            "auth": "NapCat WebUI token 无效",
            "no_credential": "未配置账密，仅重启",
            "not_ready": "QQ 客户端未到登录页",
            "hold": "处于密码失败静默期",
        }.get(kind, "登录恢复失败")
        tail = f"，人工接管：{url}" if url else ""
        return f"{head}（{message}）{tail}" if message else head + tail

    def _hold_remaining(self) -> float:
        try:
            until = float(self.state.get("qq.auth_hold_until") or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, until - time.time())

    async def _on_success(self) -> None:
        self.state.set("qq_fail_streak", 0)
        self.state.set("qq.auth_hold_until", 0)
        self.state.set("qq.last_success_at", time.time())
        # 成功同样必须立即落盘（与失败出口对称）。否则进程紧接着被强杀时，
        # 磁盘上会留着旧的 `qq.auth_hold_until`，重启后即使 QQ 已恢复，下一轮
        # 掉线也会被判"静默期内跳过"，白等最多 `password_fail_hold_sec`。
        await self.state.flush(force=True)

    # ================================================================== #
    # 通知与落盘
    # ================================================================== #
    async def _is_online(self) -> bool:
        """尽力判断 QQ 是否在线；判断不了就按"离线"处理（宁可不发也不误拉黑）。"""
        try:
            return bool((await self.napcat.get_qq_login_info()).get("online"))
        except NapCatError:
            return False

    def _notifier(self):
        return getattr(self.engine, "notifier", None)

    async def _push(self, text: str) -> None:
        notifier = self._notifier()
        if notifier is None:
            return
        try:
            # count_failure=False：登录类通知失败**不得**累计 UMO 失败次数，
            # 否则一次登录故障会把管理员永久拉黑（B17 §11.1 / 缺陷 P4）。
            await notifier.push(text, severity=Severity.WARN, force=True,
                                target="qq", status="login", count_failure=False)
        except TypeError:  # 旧签名兜底（未升级的 notifier）
            await notifier.push(text, severity=Severity.WARN, force=True,
                                target="qq", status="login")
        except Exception as exc:  # noqa: BLE001 —— 通知失败不影响动作结论
            logger.debug(f"[guardian] 登录通知失败: {exc}")

    async def _notify_ok(self, steps: list[str]) -> None:
        await self._push("[守护] QQ 登录已恢复：" + "；".join(steps[-3:]))

    def _write_error_file(self, text: str) -> None:
        """落盘供 WebUI 登录状态页读取（降级路径，B17 §11.2）。"""
        data_dir = str(getattr(self.config, "data_dir", "") or "")
        if not data_dir:
            return
        try:
            path = Path(data_dir) / "guardian" / "last_login_error.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n",
                            encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            logger.debug(f"[guardian] 写入 last_login_error.txt 失败: {exc}")

    # ================================================================== #
    def _result(self, ok: bool, detail: str, started: float) -> ActionResult:
        return ActionResult(ok, detail, int((time.time() - started) * 1000))
