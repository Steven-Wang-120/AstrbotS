"""Guardian 守护系统 —— AstrBot 插件入口。

职责边界：本文件只做 AstrBot 适配（命令注册 / LLM 工具注册 / 钩子 / 生命周期），
全部业务逻辑位于 `runtime.py` 与 `tools/llm_tools.py`。

关键约束（P1）：`observe_message` 只记录"有人在说话"这一事实（`note_event`
写入 `last_event_ts`），**不解析任何对话内容**。作息窗口的 `recent_talk` 判定
完全依赖这个时间戳，因此本文件永远不需要理解用户在说什么。

对应文档：B01 §main.py、B09（LLM 工具）、B15 §3.4（提示词注入）、B15 §7.5（作息窗口）
"""

from __future__ import annotations

import asyncio
import contextlib
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
PLUGIN_VERSION = "0.8.0"

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
/guardian announce [sleep|deferred|resume]  预览"Bot 现在会怎么说"（不发送）
/guardian drowsy                 查看"Bot 现在困不困"（就是它被问到时拿到的数据）
/guardian reset                  复位内核自修复额度（管理员）
/guardian help                   本帮助
别名：/守护
说明：本插件不会根据对话关键词触发任何动作；所有动作来自定时巡检、
生命周期等级推进或 Bot 自主调用工具。

关于"Bot 困不困"：
  直接在聊天里问它「你困吗」「还能撑吗」就行，它会调 guardian_drowsy
  拿到自己的真实状态，再按人格回你。**插件不解析任何关键词**，
  是否调用工具完全由模型自己判断。这个查看路径只是方便你在后台确认
  它此刻拿到的是什么数据。

关于"Bot 自己去说要睡了"：
  到达 L3（准备睡眠）且真睡得成、或 L4（强制睡眠）时，Bot 会主动发一条
  消息告诉管理员。**说什么由模型当场决定**，插件只负责提供事实（在线多久、
  几级了、睡多久、离窗口还有多远）和一段任务说明。三个模板可以在配置的
  lifecycle 段里改写（sleepy_prompt / sleepy_deferred_prompt / resume_prompt），
  留空就是用内置的。睡醒后也会自动报一句（post_sleep_announce）。
  它不是定时的——等级只由运行时长决定，作息窗口只决定"能不能睡"，不会
  一到夜里就让 Bot 说自己困了。"""


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


def _place_text(umo: str) -> str:
    """把 UMO 描述成"群里 / 私聊"，供提示词里提到场景用。

    不把群号发给模型：它不需要，而且那句话最终会出现在聊天记录里。
    """
    lowered = str(umo or "").lower()
    if "group" in lowered:
        return "群里"
    if "friend" in lowered or "private" in lowered or "direct" in lowered:
        return "私聊"
    return ""


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
        #: 主动消息轮询任务。`None` = 没在跑（未启动 / 已 terminate）。
        self._announce_task: asyncio.Task | None = None

    # ================================================================== #
    # 生命周期钩子
    # ================================================================== #
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        try:
            await self.runtime.start()
            self._started = True
            self._start_announce_loop()
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
            self._start_announce_loop()
            # 重载路径同样要保证状态页在（register_web_api 对同路由是替换语义）。
            self._register_webapi()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[guardian] 启动失败: {exc}", exc_info=True)

    async def terminate(self):
        """插件卸载 / 停用 / 重载时调用：严格清理，避免任务残留（R10）。"""
        # 先停轮询，再停运行时：反过来的话轮询会在 runtime 已关连接后
        # 还去 call 一次内核，每次重载都刷一串"内核不可达"告警。
        self._cancel_announce_loop()
        try:
            await self.runtime.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 停止异常: {exc}")
        self._started = False

    # ================================================================== #
    # 主动消息：Bot 自己去跟管理员说一声要睡了
    # ================================================================== #
    # 分工（重要）：
    #   - **睡前那句**由内核在真睡眠之前同步发出（`kernel_runtime.
    #     _announce_before_sleep`）。它跟睡眠动作在同一个调用栈里，不存在
    #     "刚好错过"的问题，这是可靠性最高的位置。
    #   - **这个轮询**只负责内核管不到的两件事：睡醒报备、"还没睡成"报备。
    #     它们对时效不敏感，晚一分钟说完全没影响。
    ANNOUNCE_POLL_SEC = 120.0
    #: 启动后先等一会儿：插件刚起来时 uptime 探针还没跑过，`sleepy_facts`
    #: 会因为"uptime 不可信"返回空——不是错误，但立刻轮询会白刷一轮日志。
    ANNOUNCE_WARMUP_SEC = 30.0

    def _start_announce_loop(self) -> None:
        if self._announce_task is not None and not self._announce_task.done():
            return
        self._announce_task = asyncio.create_task(self._announce_loop())

    def _cancel_announce_loop(self) -> None:
        task = self._announce_task
        self._announce_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _announce_loop(self) -> None:
        try:
            await asyncio.sleep(self.ANNOUNCE_WARMUP_SEC)
            while True:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._announce_once()
                await asyncio.sleep(self.ANNOUNCE_POLL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 轮询线程绝不能死
            logger.warning(f"[guardian] 主动消息循环异常退出: {exc}")

    async def _announce_preview(self, args: list[str]) -> str:
        """预览"Bot 现在会说的话"，**不发送、不占坑、不改任何状态**。

        输出纪律（这是本命令存在的前提）：默认**只输出那句话本身**。
        不加标题、不加事件键、不加"收件人: xxx"、不加生命周期状态块——
        那些是运维信息，塞进来就变成"一个 bot 突然开始念排障报告"，
        而这套功能的全部意义恰恰是让它像人。

        要看内部信息请显式加 `debug`：

            /guardian announce            只给那句话
            /guardian announce resume     指定要预览哪种事件
            /guardian announce debug      附带开关/收件人/事件键

        为什么要留预览：真触发要等 600 小时 uptime，正常人不会等 25 天去
        验证一次；没有它，这套功能只能靠读代码来确认。
        """
        rt = self.runtime
        want = ""
        debug = False
        for a in args:
            low = a.lower()
            if low in ("debug", "-v", "--debug"):
                debug = True
            elif low in ("sleep", "deferred", "resume"):
                want = low

        cfg = await rt.announce_config()
        peer = await rt.peer_stats()
        umo = str(peer.get("umo") or "")

        candidates: list[dict[str, Any]] = []
        if want in ("", "sleep"):
            f = await rt.sleepy_facts()
            if f and f.get("kind") == "sleep":
                candidates.append(f)
        if want in ("deferred",):
            # deferred 只在 always 模式下才会返回；这里不改配置，
            # 拿不到就如实说，而不是伪造一次。
            f = await rt.sleepy_facts()
            if f and f.get("kind") == "deferred":
                candidates.append(f)
        if want in ("", "resume"):
            f = await rt.resume_facts()
            if f:
                candidates.append(f)

        real_hit = bool(candidates)
        if not real_hit:
            # 没有真实事件时**仍然生成**：预览的全部意义就是"别等 25 天"，
            # 早先这里直接回一句"现在没有要说的"，等于在最需要看效果的时候
            # 把门关上了（用户在 QQ 里敲 announce sleep 就撞在这里）。
            # 现在走 `preview_facts`：假设等级已到，其余用真实时钟/配置。
            kind = want or "sleep"
            fake = await rt.preview_facts(kind)
            if fake:
                candidates.append(fake)

        if not candidates:
            if not debug:
                return "现在没有要说的。"
            st = await rt.lifecycle_status_text()
            return "\n".join([
                "现在没有可预览的事件。",
                "",
                f"开关: {cfg.get('mode')} | 睡醒报备: "
                f"{'开' if cfg.get('post_sleep') else '关'} | "
                f"没人应上限: {cfg.get('max_unanswered')}",
                f"收件人: {umo or '(暂无，Bot 没收到过消息)'}"
                + (f"（{peer.get('place')}）" if peer.get("place") else ""),
                f"已发言次数(无人应): {peer.get('count')}",
                "",
                st,
            ])

        # 只取第一个：正常只会有一个候选，多个时不必把三句话都刷出来。
        facts = dict(candidates[0])
        if umo:
            facts["umo"] = umo
        try:
            text = await rt.compose_announce(facts, cfg)
        except Exception as exc:  # noqa: BLE001
            return f"（生成失败：{exc}）"
        if not text:
            return "（模型没有返回内容，检查 provider 与模板配置）"

        if not debug:
            return text
        note = "预览（假设等级已到，未发送）" if facts.get("preview") else "真实事件"
        return "\n".join([
            text,
            "",
            f"—— {facts.get('kind')} | {facts.get('event_key')} | "
            f"{note} | 发给 {umo or '(无地址)'} ——",
        ])

    async def _announce_once(self, *, force_kinds: tuple[str, ...] = ()) -> bool:
        """跑一轮：判断 -> 生成 -> 占坑 -> 发送。返回是否真发了。

        这个循环**只负责"睡醒报备"和 always 模式下的"还没睡成"报备**。
        "我要睡了"由内核在真睡眠之前同步发出（见 `kernel_runtime.
        _announce_before_sleep`）——那条路径比轮询可靠，因为它跟睡眠动作
        在同一个调用栈里，不存在"刚好错过 120 秒窗口"的问题。

        顺序是刻意排的，每一环都有具体的失败模式在背后：

        1. **先看地址**：没有收件人就别白花一次 LLM 调用；
        2. **再看自限**：说了太多次没人应就先闭嘴，也别浪费 token；
        3. **生成**：拿模板 + 事实去问模型；
        4. **成功的生成之后才 `claim_announce`**：反过来做的话，一次网络
           超时就把这次事件键烧掉，接下来一小时都不会再试（而 bot 其实
           一句没说出来）；
        5. **发送**，然后记 `note_unanswered(False)`。
        """
        rt = self.runtime
        peer = await rt.peer_stats()
        umo = str(peer.get("umo") or "")
        if not umo:
            return False

        cfg = await rt.announce_config()
        mode = str(cfg.get("mode") or "on_arm")
        if mode == "off":
            return False

        max_unanswered = int(cfg.get("max_unanswered") or 0)
        unanswered = int(peer.get("count") or 0)
        if max_unanswered > 0 and unanswered >= max_unanswered:
            # 不是错误，是设计：对着一个没人理的会话反复主动说话，
            # 比沉默更糟。任何一条用户消息都会把它清零。
            logger.debug(
                f"[guardian] 主动消息暂停：已连续 {unanswered} 次无回应"
                f"（上限 {max_unanswered}）")
            return False

        # 睡醒优先：刚回来时报一句"我回来了"比再念叨睡意更有意义。
        facts: dict[str, Any] = {}
        if not force_kinds or "resume" in force_kinds:
            facts = await rt.resume_facts()
        if not facts and (not force_kinds or "sleep" in force_kinds):
            facts = await rt.sleepy_facts()
        if not facts:
            return False

        # 睡意事件（kind=sleep）已由内核在睡前发过，轮询不再重复。
        # 只有在"内核没发成功"这种极端情况下才会走到这里，而那时
        # `announced_key` 已经占坑，下面的去重会拦掉。
        event_key = str(facts.get("event_key") or "")
        state = await rt.announce_state()
        if event_key and event_key == str(state.get("announced_key") or ""):
            return False

        facts["umo"] = umo
        facts["place"] = str(peer.get("place") or "")
        try:
            text = await rt.compose_announce(facts, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 主动消息生成异常：{exc}")
            return False
        if not text:
            return False

        if not await rt.claim_announce(event_key):
            # 另一个轮次抢先说过同一件事（重载后双循环、或用户同时点了
            # 一次强制报备）。丢弃，不重复发送。
            logger.debug(f"[guardian] 主动消息事件已由其他轮次发出：{event_key}")
            return False

        try:
            await rt._send(umo, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 主动消息发送失败：{exc}")
            return False

        await rt.note_unanswered(answered=False)
        await rt.audit_append(
            kind="announce.sent", target=str(facts.get("kind") or ""),
            detail=(f"{event_key} | {text[:120]}"), ok=True,
            actor="lifecycle", trigger="announce")
        logger.info(f"[guardian] 主动消息已发送（{facts.get('kind')} -> {umo}）：{text}")
        return True


    # ================================================================== #
    # 观测：记录消息活跃度（供 QQ 在线证据链使用，不做任何触发）
    # ================================================================== #
    @filter.event_message_type(EventMessageType.ALL)
    async def observe_message(self, event: AstrMessageEvent):
        """只记录"谁在哪个会话发过言"，**绝不解析消息内容**（P1 / B15 §6）。

        三件事，都不需要读正文：

        1. 记活跃时间（QQ 在线证据链）；
        2. 记收件地址（`notify.umo_list` 常为空，主动消息靠这个找得到人）；
        3. 有人说话就清零"说了没人应"的计数——否则昨晚连说三次之后，
           今天有人接话了反而继续沉默。
        """
        with contextlib.suppress(Exception):
            umo = ""
            place = ""
            is_admin = False
            with contextlib.suppress(Exception):
                umo = str(event.unified_msg_origin or "")
            with contextlib.suppress(Exception):
                is_admin = bool(event.is_admin())
            with contextlib.suppress(Exception):
                place = _place_text(umo)
            await self.runtime.note_event(umo=umo, is_admin=is_admin, place=place)
            if await self.runtime.note_unanswered(answered=True):
                # 上一次发出去的消息有人接了：不必再额外做什么，清零本身就是
                # 全部副作用。留着这个分支是为了让"清零"这件事在日志里可见。
                logger.debug("[guardian] 有人回应，主动消息计数已清零")

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
        if sub == "announce":
            # 预览：**不发送**，只把"现在会说什么"打出来。
            # 这是这套功能唯一诚实的自查方式——主动消息的触发条件是
            # 上线 600 小时，正常部署里没人愿意等 25 天去验证一次。
            return await self._announce_preview(args[1:])
        if sub in ("drowsy", "tired", "sleepy"):
            # 用户在聊天里问「你困吗」时，模型拿到的就是这段。
            # 放出来是为了能在后台确认事实层没算错。
            return await llm_tools.drowsy_status(self.runtime)
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
    async def guardian_drowsy(self, event: AstrMessageEvent):
        """查询自己现在**困不困**。

        当用户随口问"你困吗""困不困""想睡了吗""还能撑吗""你累不累"
        "什么时候去睡"这类关于**自己状态**的问题时调用。

        也可以在自己不确定该不该提睡觉时先查一下。
        注意：这个工具只返回状态数据，不返回要说的话——具体怎么回答
        由你按人格和当下的对话自己决定。
        """
        yield event.plain_result(await llm_tools.drowsy_status(self.runtime))

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
