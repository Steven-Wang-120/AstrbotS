"""Guardian 守护系统 —— AstrBot 插件入口。

职责边界：本文件只做 AstrBot 适配（命令注册 / LLM 工具注册 / 钩子 / 生命周期），
全部业务逻辑位于 `runtime.py` 与 `tools/llm_tools.py`。

关键约束（P1）：`observe_message` 只记录"有人在说话"这一事实（`note_event`
写入 `last_event_ts`），**不解析任何对话内容**。作息窗口的 `recent_talk` 判定
完全依赖这个时间戳，因此本文件永远不需要理解用户在说什么。

对应文档：B01 §main.py、B09（LLM 工具）、B15 §3.4（提示词注入）、B15 §7.5（作息窗口）
"""

from __future__ import annotations

import os
import sys
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

try:
    # AstrBot 4.2x：filter 是独立子模块，EventMessageType/装饰器都在其下
    from astrbot.api.event.filter import EventMessageType
except ImportError:  # pragma: no cover - 兼容更早/非标准布局
    from astrbot.api.event import EventMessageType  # type: ignore
import contextlib

from astrbot.api.star import Context, Star

# 说明：AstrBot 自 v3.5.19 起不再需要 `@register` 装饰器——它会自动识别继承
# `Star` 的类作为插件类，插件元数据以 metadata.yaml 为准。旧装饰器已被官方
# 标记 DEPRECATED，故此处不再使用。

# --------------------------------------------------------------------------- #
# 导入运行时（相对导入失败时回退为显式包导入）
# --------------------------------------------------------------------------- #
try:
    from .runtime import GuardianRuntime
    from .tools import llm_tools
except ImportError:  # pragma: no cover
    import importlib
    import importlib.util

    _base = os.path.dirname(os.path.abspath(__file__))
    _pkg = os.path.basename(_base)
    if _pkg not in sys.modules:
        _spec = importlib.util.spec_from_file_location(
            _pkg, os.path.join(_base, "__init__.py"),
            submodule_search_locations=[_base])
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_pkg] = _mod
    GuardianRuntime = importlib.import_module(f"{_pkg}.runtime").GuardianRuntime
    llm_tools = importlib.import_module(f"{_pkg}.tools.llm_tools")

PLUGIN_NAME = "astrbot_plugin_guardian"
PLUGIN_DESC = "守护系统：服务器/ROS2 双模式健康守护（容器自愈 / QQ 账密登录恢复 / 内存防护 / 自主睡眠）"
PLUGIN_AUTHOR = "AstrbotS"
PLUGIN_VERSION = "0.6.0"

HELP_TEXT = """== 守护系统 Guardian ==
/guardian status                 健康快照（含运行时长与生命周期等级）
/guardian env                    运行环境（系统/内核/架构/内存，宿主机侧采集）
/guardian check [all|containers|qq|ros2]   立即巡检
/guardian level                  生命周期等级详情
/guardian sleep [分钟] [原因]    立即执行一次真睡眠（管理员）
/guardian login                  立即执行一次登录恢复（管理员）
/guardian webui                  NapCat WebUI 直达链接（免输 token）
/guardian mode <server|ros2>     切换守护模式
/guardian profile <normal|night|maintenance|aggressive>  切换档位
/guardian restart <容器>         重启容器（管理员）
/guardian incidents [n]          最近事件
/guardian diagnose <目标|all>    诊断
/guardian jobs                   查看下次任务计划
/guardian safety                 查看安全自检结果
/guardian reset                  复位内核自修复额度（管理员）
/guardian help                   本帮助
别名：/守护
说明：本插件不会根据对话关键词触发任何动作；所有动作来自定时巡检、
生命周期等级推进或 Bot 自主调用工具。"""


def _event_text(event: AstrMessageEvent) -> str:
    """跨版本取消息文本。

    AstrBot 4.x 暴露的是方法 get_message_str()，早期版本才是 message_str 属性。
    插件曾经直接读 event.message_str，在 4.26 上会 AttributeError。
    """
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            return getter() or ""
        except Exception:  # noqa: BLE001
            return ""
    return getattr(event, "message_str", "") or ""


class GuardianPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        try:
            raw = dict(config) if config is not None else {}
        except (TypeError, ValueError):
            raw = {}
        self.raw_config = raw
        self.runtime = GuardianRuntime(context, raw)
        self._started = False

    # ================================================================== #
    # 生命周期钩子
    # ================================================================== #
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        try:
            await self.runtime.start()
            self._started = True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[guardian] 启动失败: {exc}", exc_info=True)
        self._register_webapi()

    def _register_webapi(self) -> None:
        """注册 WebUI 登录状态页（B17 §10.2）。

        `register_web_api` 是 AstrBot 4.5.7+ 才有的能力，且签名/返回值在不同
        版本上可能不同——所以整块 try/except，失败就降级为落盘静态页（§10.3），
        绝不让"页面注册失败"把插件启动带崩（风险 R3）。
        """
        try:
            from .webapi import login_page
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 无法加载登录状态页模块: {exc}")
            return
        try:
            if login_page.register(self.context, self.runtime):
                logger.info("[guardian] 登录状态页已注册：/api/plug/"
                            f"{login_page.ROUTE_LOGIN}")
                return
            logger.info("[guardian] 当前 AstrBot 不支持 register_web_api，登录状态页降级落盘")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 登录状态页注册失败（降级落盘）: {exc}")
        try:
            path = login_page.write_fallback(self.runtime)
            if path:
                logger.warning(f"[guardian] 登录状态页已写入宿主机可见路径: {path}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[guardian] 登录状态页落盘失败: {exc}")

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata: Any = None):
        """插件加载/重载完成时兜底启动运行时。

        on_astrbot_loaded 只在 AstrBot 框架启动时派发一次；在 WebUI 里重载插件
        不会触发它，导致"重载成功但守护系统完全没跑"。因此这里必须再兜一次底。
        runtime.start() 自带幂等（_started 标志），重复调用无副作用。
        """
        try:
            name = ""
            if metadata is not None:
                name = str(getattr(metadata, "name", "") or "")
            if name and name != PLUGIN_NAME:
                return  # 别的插件加载完成，与本次无关
            if not self._started:
                await self.runtime.start()
                self._started = True
            # 重载路径同样要保证状态页在（register_web_api 对同路由是替换语义）。
            self._register_webapi()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[guardian] 启动失败: {exc}", exc_info=True)

    async def terminate(self):
        """插件卸载 / 停用 / 重载时调用：严格清理，避免任务残留（R10）。"""
        try:
            await self.runtime.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 停止异常: {exc}")
        self._started = False

    # ================================================================== #
    # 观测：记录消息活跃度（供 QQ 在线证据链使用，不做任何触发）
    # ================================================================== #
    @filter.event_message_type(EventMessageType.ALL)
    async def observe_message(self, event: AstrMessageEvent):
        with contextlib.suppress(Exception):
            await self.runtime.note_event()

    # ================================================================== #
    # 命令入口
    # ================================================================== #
    @filter.command("guardian", alias={"守护"})
    async def cmd_guardian(self, event: AstrMessageEvent):
        """守护系统主命令。用法见 /guardian help"""
        text = _event_text(event).strip()
        parts = text.split()
        args = parts[1:] if parts else []
        try:
            reply = await self._dispatch_command(event, args)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[guardian] 命令执行异常: {exc}", exc_info=True)
            reply = f"命令执行出错：{exc}"
        yield event.plain_result(reply)

    async def _dispatch_command(self, event: AstrMessageEvent, args: list[str]) -> str:
        rt = self.runtime
        sub = (args[0].lower() if args else "status")

        if sub in ("help", "?", "-h", "--help"):
            return HELP_TEXT
        if sub == "status":
            return await rt.status_text()
        if sub == "env":
            # 运行环境指纹：系统 / 内核 / 架构 / 内存 / 时区。
            # 取代原先前端手填的 `os_hint`——值来自宿主机侧采集，不是用户输入。
            info = await rt.env_info()
            if not info:
                return "环境信息暂不可用（采集失败）。"
            return "\n".join([
                "== 运行环境 ==",
                f"系统: {info.get('distro') or info.get('os') or 'unknown'}",
                f"发行版 ID: {info.get('os_id') or '(无)'}",
                f"内核: {info.get('kernel') or '(未知)'}",
                f"架构: {info.get('arch') or '(未知)'}"
                f" | CPU: {info.get('cpu_count') or '?'} 核"
                f" | 内存: {info.get('mem_total_mb') or 0:.0f}MB",
                f"Python: {info.get('python') or '?'}"
                f" | 时区偏移: {info.get('tz_offset_h')}h",
                f"来源: {info.get('source') or 'unknown'}",
            ])
        if sub == "check":
            scope = args[1].lower() if len(args) > 1 else "all"
            return await llm_tools.run_check(rt, scope)
        if sub == "level":
            return await rt.lifecycle_text()
        if sub == "sleep":
            if not event.is_admin():
                return "需要管理员权限。"
            duration = 0
            reason = "管理员手动触发睡眠"
            rest = args[1:]
            if rest:
                try:
                    duration = int(rest[0])
                    rest = rest[1:]
                except ValueError:
                    pass
            if rest:
                reason = " ".join(rest)
            ok, detail = await rt.request_sleep(
                reason, duration, actor="command", trigger="command",
                bypass_confirm=True)
            return f"睡眠{'已执行' if ok else '未执行'}：{detail}"
        if sub == "login":
            if not event.is_admin():
                return "需要管理员权限。"
            ok, detail = await rt.request_qq_login("管理员手动触发登录恢复")
            return f"登录恢复{'成功' if ok else '未成功'}：{detail}"
        if sub == "webui":
            # ⚠️ 回执里带 webui token = token 进了 QQ 聊天记录。B17 §9.3 允许
            # 这个行为，但**只对管理员**放开：把协议端凭据发给群里任何人都等于
            # 交出 NapCat 控制台（与 /guardian restart 同级权限，故同级鉴权）。
            if not event.is_admin():
                return "需要管理员权限。"
            # 审计只记 kind，**不记该文本**（B17 §9.3）。
            await rt.audit_append(kind="qq.webui_link", target="qq",
                                  detail="管理员查看 NapCat WebUI 直达链接",
                                  actor="command", trigger="command", ok=True)
            return rt.webui_links()
        if sub == "mode":
            if not event.is_admin():
                return "需要管理员权限。"
            if len(args) < 2:
                return "用法：/guardian mode <server|ros2>"
            return await llm_tools.set_mode(rt, args[1], is_admin=True)
        if sub == "profile":
            if not event.is_admin():
                return "需要管理员权限。"
            if len(args) < 2:
                return "用法：/guardian profile <normal|night|maintenance|aggressive>"
            return await llm_tools.set_profile(rt, args[1], is_admin=True)
        if sub == "restart":
            if not event.is_admin():
                return "需要管理员权限。"
            allow = await rt.allowlist()
            if len(args) < 2:
                return (f"用法：/guardian restart <容器>。"
                        f"允许：{', '.join(allow) or '(白名单为空)'}")
            return await llm_tools.restart_container(
                rt, args[1], reason="管理员手动重启", is_admin=True, confirm=True)
        if sub == "incidents":
            limit = 10
            if len(args) > 1:
                with contextlib.suppress(ValueError):
                    limit = int(args[1])
            return await rt.incidents_text(limit)
        if sub == "diagnose":
            target = args[1] if len(args) > 1 else "all"
            return await rt.diagnose(target)
        if sub == "jobs":
            return "下次任务计划：\n" + await rt.jobs_text()
        if sub == "safety":
            warns = await rt.safety_warnings()
            if not warns:
                return "安全自检：无告警。"
            return "安全自检告警：\n" + "\n".join(f"  - {w}" for w in warns)
        if sub == "reset":
            if not event.is_admin():
                return "需要管理员权限。"
            return await rt.reset_kernel_health()
        return f"未知子命令 {sub}。\n{HELP_TEXT}"

    # ================================================================== #
    # LLM 工具（由 LLM 自主决定调用；插件不解析任何对话关键词）
    # ================================================================== #
    @filter.llm_tool()
    async def guardian_health_report(self, event: AstrMessageEvent, scope: str = "all"):
        """获取守护系统的健康报告（只读）。

        当你需要了解服务器/容器/QQ/ROS2 当前是否健康时调用。
        用户询问"系统还好吗""容器状态""QQ 在线吗"等运维相关问题时也适合调用。

        Args:
            scope(string): 检查范围，可选值：all（全部）、containers（容器）、qq（QQ 协议端）、ros2（ROS2）
        """
        yield event.plain_result(await llm_tools.health_report(self.runtime, scope))

    @filter.llm_tool()
    async def guardian_lifecycle_status(self, event: AstrMessageEvent):
        """查询本守护系统的生命周期状态：已连续运行多久、当前等级、距离强制睡眠还有多久。

        当用户问"你运行多久了""什么时候休息""睡眠机制"时调用。
        """
        yield event.plain_result(await llm_tools.lifecycle_status(self.runtime))

    @filter.llm_tool()
    async def guardian_run_check(self, event: AstrMessageEvent, scope: str = "all"):
        """立即执行一次健康巡检，并返回发现的异常（可能触发自动修复动作）。

        当用户要求"现在检查一下""排查一下"时调用。

        Args:
            scope(string): 巡检范围，可选值：all、containers、qq、ros2
        """
        yield event.plain_result(await llm_tools.run_check(self.runtime, scope))

    @filter.llm_tool()
    async def guardian_diagnose(self, event: AstrMessageEvent, target: str = "all"):
        """对指定目标做诊断，输出该目标的探针指标明细（只读）。

        Args:
            target(string): 目标名，例如容器名 astrbot/napcat，或 all 表示全部
        """
        yield event.plain_result(await llm_tools.diagnose(self.runtime, target))

    @filter.llm_tool()
    async def guardian_list_incidents(self, event: AstrMessageEvent, limit: float = 10):
        """查看最近的守护事件（告警与已采取的动作）。

        Args:
            limit(number): 返回条数，1-50，默认 10
        """
        # 不要在这里 int(limit)：LLM 可能传 Infinity/NaN，int(float('inf')) 抛
        # OverflowError。统一交给 llm_tools.safe_int 做有限性校验与夹取。
        yield event.plain_result(await llm_tools.list_incidents(self.runtime, limit))

    @filter.llm_tool()
    async def guardian_request_sleep(self, event: AstrMessageEvent, reason: str,
                                     duration_min: float = 0,
                                     confirm: bool = False):
        """请求让自己"睡一觉"——即执行一次真实的容器重启维护，服务会短暂中断后恢复。

        这是你（Bot）自主决定休息的方式。当系统已连续运行很久、或你自己觉得
        需要一次彻底休息/重置时，可以主动调用本工具。
        注意：不要因为用户随口提到"睡觉"就调用；应由你自己判断是否真的需要休息。
        破坏性二次确认：首次调用（confirm=false）只返回确认提示，不执行；
        你确认无误后再次调用并置 confirm=true 才会真正执行。

        Args:
            reason(string): 请求休息的原因，会记录到审计日志并告知用户
            duration_min(number): 期望离线时长（分钟），0-120，0 表示立即重启恢复
            confirm(boolean): 二次确认标志，true 才真正执行，默认 false
        """
        armed = await self.runtime.sleep_armed()
        text = await llm_tools.request_sleep(
            self.runtime, reason, duration_min,
            is_admin=event.is_admin(), armed=armed, confirm=confirm)
        yield event.plain_result(text)

    @filter.llm_tool()
    async def guardian_restart_container(self, event: AstrMessageEvent,
                                         container: str, confirm: bool = False,
                                         reason: str = ""):
        """重启一个受管容器（破坏性操作，需要管理员权限，且容器必须在白名单内）。

        仅在用户明确要求重启、或你确认必须重启时调用。
        首次调用（confirm=false）只返回确认提示，不执行动作；确认无误后
        再次调用并置 confirm=true 才会真正执行。

        Args:
            container(string): 容器名，必须在动作白名单内（例如 astrbot、napcat）
            confirm(boolean): 二次确认标志，true 才真正执行，默认 false
            reason(string): 重启原因
        """
        yield event.plain_result(await llm_tools.restart_container(
            self.runtime, container, reason, is_admin=event.is_admin(),
            confirm=confirm))

    @filter.llm_tool()
    async def guardian_set_mode(self, event: AstrMessageEvent, mode: str):
        """切换守护模式（需要管理员权限）。

        Args:
            mode(string): 模式，可选值：server（服务器/容器守护）、ros2（ROS2 节点话题守护）
        """
        yield event.plain_result(await llm_tools.set_mode(
            self.runtime, mode, is_admin=event.is_admin()))

    @filter.llm_tool()
    async def guardian_set_profile(self, event: AstrMessageEvent, profile: str):
        """切换守护档位（需要管理员权限），影响巡检频率与告警静默策略。

        Args:
            profile(string): 档位，可选值：normal（常规）、night（夜间，省资源）、maintenance（维护，暂停巡检）、aggressive（激进，更敏感）
        """
        yield event.plain_result(await llm_tools.set_profile(
            self.runtime, profile, is_admin=event.is_admin()))

    # ================================================================== #
    # LLM 请求钩子：注入生命周期提示词与状态上下文
    # ================================================================== #
    @filter.on_llm_request()
    async def inject_guardian_context(self, event: AstrMessageEvent, req: Any):
        """把生命周期引导提示词注入到 LLM 请求。

        注意：不使用 system_prompt（会破坏上游缓存），改用 extra_user_content_parts。
        素材由内核一次性给出（`llm_injection`）——适配层不去读内核的
        `state`/`config`，否则在 `kernel.mode=ipc` 下会直接违反 I2。
        """
        try:
            injection = await self.runtime.llm_injection()
            text = str(injection.get("text") or "")
            if not text:
                return
            mode = str(injection.get("mode") or "extra_user_content")
            if mode == "system_prompt":
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + text
            else:
                parts = getattr(req, "extra_user_content_parts", None)
                if parts is None:
                    return
                try:
                    from astrbot.core.agent.message import TextPart
                    parts.append(TextPart(text=text))
                except Exception:  # noqa: BLE001
                    # 兜底：退化为 system_prompt
                    req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + text
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[guardian] 上下文注入失败: {exc}")

    @filter.on_using_llm_tool()
    async def on_using_tool(self, event: AstrMessageEvent, tool: Any, tool_args: Any):
        name = getattr(tool, "name", "")
        if str(name).startswith("guardian_"):
            llm_tools.log_tool_call(str(name), tool_args if isinstance(tool_args, dict) else None)
            await self.runtime.audit_append(
                kind="llm.tool_call", target=str(name),
                detail=str(tool_args)[:300], actor="llm", trigger="llm", ok=True)

    @filter.on_llm_tool_respond()
    async def on_tool_respond(self, event: AstrMessageEvent, tool: Any,
                              tool_args: Any, tool_result: Any):
        name = getattr(tool, "name", "")
        if str(name).startswith("guardian_"):
            await self.runtime.audit_append(
                kind="llm.tool_result", target=str(name),
                detail=str(tool_result)[:300], actor="llm", trigger="llm", ok=True)
